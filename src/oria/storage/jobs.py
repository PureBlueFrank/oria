"""Tenant-scoped durable admission and compare-and-set state/event transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.job_leases import (
    JobLeaseToken,
    LeaseOperation,
    validate_claim,
    validate_lease,
    validate_lease_change,
)
from oria.core.jobs import (
    Job,
    JobConflict,
    JobSession,
    JobSubmission,
    JobUnavailable,
    submission_json,
    validate_transition,
)
from oria.storage.database import set_session_tenant_context

Guard = Callable[[], Awaitable[None]]
_UPDATE_COLUMNS = (
    "status",
    "revision",
    "attempt",
    "lease_owner",
    "lease_epoch",
    "lease_expires_at",
    "heartbeat_at",
    "retry_at",
    "cancel_requested",
    "accepted_checkpoint_ns",
    "accepted_checkpoint_id",
    "updated_at",
)


def _values(job: Job) -> dict[str, Any]:
    return job.model_dump(exclude={"submission"}) | {
        "request_json": submission_json(job.submission),
        "scenario": job.submission.spec.scenario,
    }


def _job(row: Mapping[str, Any]) -> Job:
    values = dict(row)
    submission = JobSubmission.model_validate_json(values.pop("request_json"))
    if values.pop("scenario") != submission.spec.scenario:
        raise JobUnavailable("job snapshot unavailable")
    return Job.model_validate(values | {"submission": submission})


class JobRepository:
    """SQL shared by SQLite and PostgreSQL; SQLite deployment remains single worker.

    Guards recheck current trusted identity and local Policy without opening a second
    audit writer inside this transaction. Successful authorization audits precede it.
    No claim, background executor or fencing guarantee is implemented by admission.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _hold_execution_lease(
        self, expected: Job, token: JobLeaseToken, clock: Callable[[], datetime]
    ) -> AsyncIterator[AsyncSession]:
        """SQLite write lock held through the caller's Business DB commit/rollback.

        No Job transition is committed. This serializes cooperating DB writers;
        it is not a distributed transaction across the two databases.
        """
        async with self._sessions() as session:
            if session.bind is None or session.bind.dialect.name != "sqlite":
                raise ValueError("execution fencing currently requires SQLite")
            values = _values(expected)
            predicate = " AND ".join(
                f"{column} IS NOT DISTINCT FROM :{column}" for column in values
            )
            try:
                locked = (
                    await session.execute(
                        text(
                            f"UPDATE jobs SET revision = revision WHERE {predicate} "
                            "RETURNING job_id"
                        ),
                        values,
                    )
                ).scalar_one_or_none()
                if locked is None:
                    raise JobConflict("job revision changed before execution")
                validate_lease(expected, token, clock())
                if expected.status != "running" or expected.cancel_requested:
                    raise JobConflict("job cannot execute a business write")
                yield session
            except SQLAlchemyError:
                raise JobUnavailable("job execution lock unavailable") from None
            finally:
                await session.rollback()

    async def create_session(self, value: JobSession, guard: Guard) -> JobSession:
        try:
            async with self._sessions.begin() as session:
                await set_session_tenant_context(session, value.tenant_id)
                await guard()
                await session.execute(
                    text(
                        "INSERT INTO sessions (tenant_id, session_id, requester_subject_id, "
                        "requester_issuer, requester_external_subject, created_at) VALUES "
                        "(:tenant_id, :session_id, :requester_subject_id, :requester_issuer, "
                        ":requester_external_subject, :created_at)"
                    ),
                    value.model_dump(),
                )
                await guard()
        except SQLAlchemyError:
            raise JobUnavailable("session persistence unavailable") from None
        return value

    async def get_session(self, tenant_id: str, session_id: str) -> JobSession | None:
        try:
            async with self._sessions() as session:
                await set_session_tenant_context(session, tenant_id)
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM sessions WHERE tenant_id = :tenant "
                                "AND session_id = :session"
                            ),
                            {"tenant": tenant_id, "session": session_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                return None if row is None else JobSession.model_validate(dict(row))
        except SQLAlchemyError:
            raise JobUnavailable("session read unavailable") from None

    async def get(self, tenant_id: str, job_id: str) -> Job | None:
        try:
            async with self._sessions() as session:
                await set_session_tenant_context(session, tenant_id)
                return await self._read(session, tenant_id, job_id)
        except (SQLAlchemyError, ValueError):
            raise JobUnavailable("job read unavailable") from None

    @staticmethod
    async def _read(session: AsyncSession, tenant_id: str, job_id: str) -> Job | None:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM jobs WHERE tenant_id = :tenant AND job_id = :job"),
                    {"tenant": tenant_id, "job": job_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _job(dict(row))

    async def submit(self, value: Job, guard: Guard) -> Job:
        value = Job.model_validate(value.model_dump())
        if value.status != "queued" or value.revision or value.attempt or value.lease_epoch:
            raise ValueError("new job must be unexecuted and queued")
        values = _values(value)
        columns = ", ".join(values)
        parameters = ", ".join(f":{column}" for column in values)
        try:
            async with self._sessions.begin() as session:
                await set_session_tenant_context(session, value.tenant_id)
                await guard()
                # INSERT is the first data operation: competing writers serialize here.
                created = (
                    await session.execute(
                        text(
                            f"INSERT INTO jobs ({columns}) VALUES ({parameters}) "
                            "ON CONFLICT (tenant_id, requester_subject_id, idempotency_key) "
                            "DO NOTHING RETURNING job_id"
                        ),
                        values,
                    )
                ).scalar_one_or_none() is not None
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM jobs WHERE tenant_id = :tenant_id "
                                "AND requester_subject_id = :requester_subject_id "
                                "AND idempotency_key = :idempotency_key"
                            ),
                            values,
                        )
                    )
                    .mappings()
                    .one()
                )
                result = _job(dict(row))
                if result.request_hash != value.request_hash:
                    raise JobConflict("idempotency key content conflict")
                if created:
                    await self._append_event(session, result)
                await guard()
                return result
        except SQLAlchemyError:
            raise JobUnavailable("job persistence unavailable") from None

    async def replace(
        self,
        expected: Job,
        replacement: Job,
        guard: Guard,
        *,
        idempotent_cancel: bool = False,
    ) -> Job:
        replacement = Job.model_validate(replacement.model_dump())
        validate_transition(expected, replacement)
        return await self._replace(
            expected, replacement, guard, idempotent_cancel=idempotent_cancel
        )

    async def replace_leased(
        self,
        expected: Job,
        replacement: Job,
        guard: Guard,
        *,
        operation: LeaseOperation,
        token: JobLeaseToken | None,
        clock: Callable[[], datetime],
    ) -> Job:
        expected = Job.model_validate(expected.model_dump())
        replacement = Job.model_validate(replacement.model_dump())
        validate_lease_change(expected, replacement, operation, token)

        async def fenced_guard() -> None:
            now = clock()
            if operation == "claim":
                validate_claim(expected, now)
                if replacement.lease_expires_at is None or replacement.lease_expires_at <= now:
                    raise JobConflict("new lease expired before submission")
            else:
                assert token is not None
                validate_lease(expected, token, now)
            await guard()
            # A suspended authorization callback cannot outlive the current lease.
            now = clock()
            if operation == "claim":
                validate_claim(expected, now)
                if replacement.lease_expires_at is None or replacement.lease_expires_at <= now:
                    raise JobConflict("new lease expired before submission")
            else:
                assert token is not None
                validate_lease(expected, token, now)

        return await self._replace(expected, replacement, fenced_guard)

    async def _replace(
        self,
        expected: Job,
        replacement: Job,
        guard: Guard,
        *,
        idempotent_cancel: bool = False,
    ) -> Job:
        expected_values = _values(expected)
        values = _values(replacement)
        predicate = " AND ".join(
            f"{column} IS NOT DISTINCT FROM :expected_{column}" for column in expected_values
        )
        assignments = ", ".join(f"{column} = :{column}" for column in _UPDATE_COLUMNS)
        parameters = values | {f"expected_{key}": val for key, val in expected_values.items()}
        try:
            async with self._sessions.begin() as session:
                await set_session_tenant_context(session, expected.tenant_id)
                await guard()
                updated = (
                    await session.execute(
                        text(f"UPDATE jobs SET {assignments} WHERE {predicate} RETURNING job_id"),
                        parameters,
                    )
                ).scalar_one_or_none()
                if updated is None:
                    current = await self._read(session, expected.tenant_id, expected.job_id)
                    if (
                        idempotent_cancel
                        and current is not None
                        and current.status in {"cancelled", "cancellation_requested"}
                        and current.request_hash == expected.request_hash
                    ):
                        await guard()
                        return current
                    raise JobConflict("job revision changed")
                await self._append_event(session, replacement)
                await guard()
                return replacement
        except SQLAlchemyError:
            raise JobUnavailable("job transition unavailable") from None

    @staticmethod
    async def _append_event(session: AsyncSession, job: Job) -> None:
        await session.execute(
            text(
                "INSERT INTO job_events "
                "(tenant_id, job_id, sequence, event_type, status, created_at) "
                "VALUES (:tenant_id, :job_id, :sequence, :event_type, :status, :created_at)"
            ),
            {
                "tenant_id": job.tenant_id,
                "job_id": job.job_id,
                "sequence": job.revision,
                "event_type": f"job.{job.status}",
                "status": job.status,
                "created_at": job.updated_at,
            },
        )
