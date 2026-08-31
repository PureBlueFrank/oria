"""SQLite persistence for Business DB execution-ledger records and event facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.types import EventEnvelope
from oria.domain.ledger import DomainEvent, ExecutionStatus, OutboxRecord, ToolExecution
from oria.permission.audit import redact_audit_payload


class LedgerRepositoryError(RuntimeError):
    """Safe persistence failure without SQL, arguments, or tenant data."""


_TOOL_COLUMNS = (
    "execution_id, tenant_id, tool_name, idempotency_key, canonical_args_hash, checkpoint_id, "
    "status, receipt_id, compensation_status, attempt_count, created_at, updated_at, executed_at"
)


def _tool_from_row(row: Mapping[str, Any]) -> ToolExecution:
    return ToolExecution.model_validate(dict(row))


class SQLiteToolExecutionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _find_by_idempotency(
        self,
        session: AsyncSession,
        tenant_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> ToolExecution | None:
        result = await session.execute(
            text(
                f"SELECT {_TOOL_COLUMNS} FROM tool_executions "
                "WHERE tenant_id = :tenant_id AND tool_name = :tool_name "
                "AND idempotency_key = :idempotency_key"
            ),
            {
                "tenant_id": tenant_id,
                "tool_name": tool_name,
                "idempotency_key": idempotency_key,
            },
        )
        row = result.mappings().one_or_none()
        return None if row is None else _tool_from_row(dict(row))

    async def _find_by_id(
        self,
        session: AsyncSession,
        tenant_id: str,
        execution_id: str,
    ) -> ToolExecution | None:
        result = await session.execute(
            text(
                f"SELECT {_TOOL_COLUMNS} FROM tool_executions "
                "WHERE tenant_id = :tenant_id AND execution_id = :execution_id"
            ),
            {"tenant_id": tenant_id, "execution_id": execution_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else _tool_from_row(dict(row))

    async def _reserve_with_status(
        self,
        execution: ToolExecution,
    ) -> tuple[ToolExecution, bool]:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        f"INSERT INTO tool_executions ({_TOOL_COLUMNS}) VALUES "
                        "(:execution_id, :tenant_id, :tool_name, :idempotency_key, "
                        ":canonical_args_hash, :checkpoint_id, :status, :receipt_id, "
                        ":compensation_status, :attempt_count, :created_at, :updated_at, "
                        ":executed_at)"
                    ),
                    execution.model_dump(),
                )
            return execution, True
        except IntegrityError as exc:
            existing = await self.get_by_idempotency(
                execution.tenant_id,
                execution.tool_name,
                execution.idempotency_key,
            )
            if existing is None:
                raise ValueError("execution identity already exists") from exc
            return existing, False
        except (LedgerRepositoryError, ValueError):
            raise
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("execution reservation failed") from exc

    async def reserve_for_request(
        self,
        execution: ToolExecution,
        request_idempotency_key: str,
    ) -> ToolExecution:
        """Atomically bind one caller request key to one canonical business execution."""
        if not request_idempotency_key:
            raise ValueError("request idempotency key is required")
        try:
            async with self._sessions.begin() as session:
                result = await session.execute(
                    text(
                        "SELECT canonical_args_hash, execution_id FROM tool_execution_requests "
                        "WHERE tenant_id = :tenant_id AND tool_name = :tool_name "
                        "AND request_idempotency_key = :request_key"
                    ),
                    {
                        "tenant_id": execution.tenant_id,
                        "tool_name": execution.tool_name,
                        "request_key": request_idempotency_key,
                    },
                )
                request_row = result.mappings().one_or_none()
                if request_row is not None:
                    if str(request_row["canonical_args_hash"]) != execution.canonical_args_hash:
                        raise ValueError("request idempotency key conflicts with canonical payload")
                    history = await self._find_by_id(
                        session,
                        execution.tenant_id,
                        str(request_row["execution_id"]),
                    )
                    if history is None:
                        raise LedgerRepositoryError("request execution binding is unavailable")
                    return history
                history = await self._find_by_idempotency(
                    session,
                    execution.tenant_id,
                    execution.tool_name,
                    execution.idempotency_key,
                )
                if history is None:
                    await session.execute(
                        text(
                            f"INSERT INTO tool_executions ({_TOOL_COLUMNS}) VALUES "
                            "(:execution_id, :tenant_id, :tool_name, :idempotency_key, "
                            ":canonical_args_hash, :checkpoint_id, :status, :receipt_id, "
                            ":compensation_status, :attempt_count, :created_at, :updated_at, "
                            ":executed_at)"
                        ),
                        execution.model_dump(),
                    )
                    history = execution
                elif history.canonical_args_hash != execution.canonical_args_hash:
                    raise ValueError("business idempotency scope conflicts with canonical payload")
                await session.execute(
                    text(
                        "INSERT INTO tool_execution_requests (tenant_id, tool_name, "
                        "request_idempotency_key, canonical_args_hash, execution_id, created_at) "
                        "VALUES (:tenant_id, :tool_name, :request_key, :args_hash, "
                        ":execution_id, :created_at)"
                    ),
                    {
                        "tenant_id": execution.tenant_id,
                        "tool_name": execution.tool_name,
                        "request_key": request_idempotency_key,
                        "args_hash": execution.canonical_args_hash,
                        "execution_id": history.execution_id,
                        "created_at": execution.created_at,
                    },
                )
                return history
        except (ValueError, LedgerRepositoryError):
            raise
        except IntegrityError as exc:
            raise ValueError("execution request identity already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("execution request reservation failed") from exc

    async def reserve(self, execution: ToolExecution) -> ToolExecution:
        record, _ = await self._reserve_with_status(execution)
        return record

    async def _transition(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        execution_id: str,
        target: Literal["executing", "succeeded", "failed", "unknown"],
        expected_status: ExecutionStatus,
        updated_at: datetime,
        receipt_id: str | None = None,
        compensation_status: str | None = None,
    ) -> ToolExecution:
        existing = await self._find_by_id(session, tenant_id, execution_id)
        if existing is None:
            raise LookupError("execution is unavailable")
        if existing.status != expected_status:
            raise ValueError("execution is not in the required source state")
        transitioned = existing.transition_to(
            target,
            updated_at=updated_at,
            receipt_id=receipt_id,
            compensation_status=compensation_status,
        )
        result = await session.execute(
            text(
                "UPDATE tool_executions SET status = :status, receipt_id = :receipt_id, "
                "compensation_status = :compensation_status, attempt_count = :attempt_count, "
                "updated_at = :updated_at, executed_at = :executed_at "
                "WHERE tenant_id = :tenant_id AND execution_id = :execution_id "
                "AND status = :expected_status"
            ),
            transitioned.model_dump() | {"expected_status": existing.status},
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise LedgerRepositoryError("execution state changed concurrently")
        return transitioned

    async def _public_transition(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        target: Literal["executing", "succeeded", "failed", "unknown"],
        expected_status: ExecutionStatus,
        updated_at: datetime,
        receipt_id: str | None = None,
        compensation_status: str | None = None,
    ) -> ToolExecution:
        try:
            async with self._sessions.begin() as session:
                return await self._transition(
                    session,
                    tenant_id=tenant_id,
                    execution_id=execution_id,
                    target=target,
                    expected_status=expected_status,
                    updated_at=updated_at,
                    receipt_id=receipt_id,
                    compensation_status=compensation_status,
                )
        except (LookupError, ValueError, LedgerRepositoryError):
            raise
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("execution state persistence failed") from exc

    async def mark_executing(
        self,
        tenant_id: str,
        execution_id: str,
        updated_at: datetime,
    ) -> ToolExecution:
        return await self._public_transition(
            tenant_id=tenant_id,
            execution_id=execution_id,
            target="executing",
            expected_status="reserved",
            updated_at=updated_at,
        )

    async def record_success(
        self,
        tenant_id: str,
        execution_id: str,
        receipt_id: str,
        updated_at: datetime,
    ) -> ToolExecution:
        return await self._public_transition(
            tenant_id=tenant_id,
            execution_id=execution_id,
            target="succeeded",
            expected_status="executing",
            receipt_id=receipt_id,
            updated_at=updated_at,
        )

    async def record_failure(
        self,
        tenant_id: str,
        execution_id: str,
        updated_at: datetime,
        *,
        compensation_status: str | None = None,
    ) -> ToolExecution:
        return await self._public_transition(
            tenant_id=tenant_id,
            execution_id=execution_id,
            target="failed",
            expected_status="executing",
            compensation_status=compensation_status,
            updated_at=updated_at,
        )

    async def record_unknown(
        self,
        tenant_id: str,
        execution_id: str,
        updated_at: datetime,
        *,
        receipt_id: str | None = None,
        compensation_status: str = "reconciliation_required",
    ) -> ToolExecution:
        return await self._public_transition(
            tenant_id=tenant_id,
            execution_id=execution_id,
            target="unknown",
            expected_status="executing",
            receipt_id=receipt_id,
            compensation_status=compensation_status,
            updated_at=updated_at,
        )

    async def get_by_idempotency(
        self,
        tenant_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> ToolExecution | None:
        try:
            async with self._sessions() as session:
                return await self._find_by_idempotency(
                    session,
                    tenant_id,
                    tool_name,
                    idempotency_key,
                )
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise LedgerRepositoryError("execution history read failed") from exc

    async def reconcile(
        self,
        tenant_id: str,
        execution_id: str,
        outcome: Literal["succeeded", "failed"],
        updated_at: datetime,
        *,
        receipt_id: str | None = None,
        compensation_status: str | None = None,
    ) -> ToolExecution:
        return await self._public_transition(
            tenant_id=tenant_id,
            execution_id=execution_id,
            target=outcome,
            expected_status="unknown",
            receipt_id=receipt_id,
            compensation_status=compensation_status,
            updated_at=updated_at,
        )


class SQLiteDomainEventRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _append(self, session: AsyncSession, domain_event: DomainEvent) -> None:
        payload = domain_event.model_dump(mode="json")["payload"]
        sanitized = redact_audit_payload(payload)
        await session.execute(
            text(
                "INSERT INTO domain_events (event_id, tenant_id, aggregate_type, aggregate_id, "
                "event_type, event_version, payload_json, occurred_at, correlation_id) VALUES "
                "(:event_id, :tenant_id, :aggregate_type, :aggregate_id, :event_type, "
                ":event_version, :payload_json, :occurred_at, :correlation_id)"
            ),
            domain_event.model_dump(exclude={"payload"})
            | {
                "payload_json": json.dumps(
                    sanitized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )

    async def append(self, domain_event: DomainEvent) -> None:
        try:
            async with self._sessions.begin() as session:
                await self._append(session, domain_event)
        except IntegrityError as exc:
            raise ValueError("domain event already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("domain event persistence failed") from exc


class SQLiteBusinessAuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _append(self, session: AsyncSession, envelope: EventEnvelope) -> None:
        if envelope.resource.tenant_id != envelope.tenant_id:
            raise ValueError("cross-tenant business audit is forbidden")
        payload = envelope.model_dump(mode="json")["payload"]
        sanitized = redact_audit_payload(payload)
        await session.execute(
            text(
                "INSERT INTO audit_events (event_id, occurred_at, tenant_id, actor, action, "
                "resource_type, resource_id, resource_tenant_id, decision, policy_version, "
                "args_hash, result, correlation_id, payload_json) VALUES (:event_id, "
                ":occurred_at, :tenant_id, :actor, :action, :resource_type, :resource_id, "
                ":resource_tenant_id, :decision, :policy_version, :args_hash, :result, "
                ":correlation_id, :payload_json)"
            ),
            {
                "event_id": envelope.event_id,
                "occurred_at": envelope.occurred_at,
                "tenant_id": envelope.tenant_id,
                "actor": envelope.actor,
                "action": envelope.action,
                "resource_type": envelope.resource.resource_type,
                "resource_id": envelope.resource.resource_id,
                "resource_tenant_id": envelope.resource.tenant_id,
                "decision": envelope.decision,
                "policy_version": envelope.policy_version,
                "args_hash": envelope.args_hash,
                "result": envelope.result,
                "correlation_id": envelope.correlation_id,
                "payload_json": json.dumps(
                    sanitized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )

    async def append(self, envelope: EventEnvelope) -> None:
        try:
            async with self._sessions.begin() as session:
                await self._append(session, envelope)
        except ValueError:
            raise
        except IntegrityError as exc:
            raise ValueError("business audit event already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("business audit persistence failed") from exc


class SQLiteOutboxRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _append(self, session: AsyncSession, record: OutboxRecord) -> None:
        await session.execute(
            text(
                "INSERT INTO outbox (event_id, tenant_id, topic, payload_json, occurred_at, "
                "available_at, published_at, attempt_count, last_error_code) VALUES (:event_id, "
                ":tenant_id, :topic, :payload_json, :occurred_at, :available_at, :published_at, "
                ":attempt_count, :last_error_code)"
            ),
            record.model_dump(),
        )

    async def append(self, record: OutboxRecord) -> None:
        try:
            async with self._sessions.begin() as session:
                await self._append(session, record)
        except IntegrityError as exc:
            raise ValueError("outbox event already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("outbox event persistence failed") from exc

    async def mark_published(self, event_id: str) -> None:
        if not event_id:
            raise ValueError("outbox event identity is required")
        try:
            async with self._sessions.begin() as session:
                result = await session.execute(
                    text(
                        "UPDATE outbox SET published_at = :published_at, "
                        "attempt_count = attempt_count + 1, last_error_code = NULL "
                        "WHERE event_id = :event_id AND published_at IS NULL"
                    ),
                    {"event_id": event_id, "published_at": datetime.now(UTC)},
                )
                if not isinstance(result, CursorResult) or result.rowcount != 1:
                    raise LookupError("pending outbox event is unavailable")
        except LookupError:
            raise
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("outbox publication update failed") from exc

    async def list_pending(self, tenant_id: str, limit: int) -> tuple[OutboxRecord, ...]:
        if not tenant_id:
            raise ValueError("tenant identity is required")
        if limit < 1:
            raise ValueError("outbox limit must be positive")
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT event_id, tenant_id, topic, payload_json, occurred_at, "
                        "available_at, published_at, attempt_count, last_error_code FROM outbox "
                        "WHERE tenant_id = :tenant_id AND published_at IS NULL "
                        "AND available_at <= :now ORDER BY available_at, event_id LIMIT :limit"
                    ),
                    {"tenant_id": tenant_id, "now": datetime.now(UTC), "limit": limit},
                )
                return tuple(OutboxRecord.model_validate(dict(row)) for row in result.mappings())
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise LedgerRepositoryError("pending outbox read failed") from exc
