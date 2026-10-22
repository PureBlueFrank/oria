"""Authorized durable job admission; HTTP never executes a Graph in the background."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.context import Context, RuntimeServices
from oria.core.jobs import (
    TERMINAL_JOB_STATUSES,
    Job,
    JobConflict,
    JobSession,
    JobSubmission,
    JobView,
    job_view,
    submission_hash,
)
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    PolicyDecision,
    ResourceRef,
    ValueModel,
)
from oria.permission.local import LocalPolicyEngine
from oria.storage.jobs import Guard, JobRepository

T = TypeVar("T")


class SessionCreateBody(ValueModel):
    """Session ownership and identifiers are always assigned by the server."""


class SessionView(ValueModel):
    session_id: str
    created_at: datetime


class _DeferredDenial(PermissionError):
    def __init__(self, request: AuthorizationRequest, ctx: Context, decision: PolicyDecision):
        super().__init__("resource unavailable")
        self.request, self.ctx, self.decision = request, ctx, decision


class JobAPI:
    def __init__(
        self,
        runtime: RuntimeServices,
        repository: JobRepository,
        directory: Callable[[], IdentityDirectory],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(runtime.policy, LocalPolicyEngine):
            raise ValueError("job admission currently requires local Policy")
        self._runtime = runtime
        self._policy = runtime.policy
        self._repository = repository
        self._directory = directory
        self._clock = clock

    def _identity(self, identity: IdentityBinding) -> None:
        if self._directory().resolve(identity.issuer, identity.subject) != identity:
            raise AuthenticationError("identity changed")

    def _context(
        self,
        identity: IdentityBinding,
        request_id: str,
        value: JobSession | Job,
    ) -> Context:
        self._identity(identity)
        return self._runtime.new_context(
            actor=identity.actor,
            executor=identity.executor,
            session_id=value.session_id,
            thread_id=value.thread_id if isinstance(value, Job) else request_id,
            run_id=request_id,
            correlation_id=request_id,
            job_id=value.job_id if isinstance(value, Job) else None,
        )

    @staticmethod
    def _owned(value: JobSession | Job | None, identity: IdentityBinding) -> None:
        if value is None or (value.tenant_id, value.requester_subject_id) != (
            identity.actor.tenant_id,
            identity.actor.subject_id,
        ):
            raise PermissionError("resource unavailable")

    @staticmethod
    def _request(action: str, ctx: Context, value: JobSession | Job) -> AuthorizationRequest:
        resource_type = "job" if isinstance(value, Job) else "session"
        if action == "campaign:draft:write":
            resource_type = "campaign"
        elif action == "analytics:read":
            resource_type = "analytics_fact"
        return AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=action,
            resource=ResourceRef(
                resource_type=resource_type,
                resource_id=value.job_id if isinstance(value, Job) else value.session_id,
                tenant_id=value.tenant_id,
            ),
            context=AuthorizationContext(
                correlation_id=ctx.correlation_id,
                attributes={
                    "requester_subject_id": value.requester_subject_id,
                    "resource_attributes": {},
                },
            ),
        )

    async def _authorize(
        self,
        identity: IdentityBinding,
        ctx: Context,
        requests: tuple[AuthorizationRequest, ...],
    ) -> Guard:
        for request in requests:
            self._identity(identity)
            decision = await self._policy.authorize(request, ctx)
            self._identity(identity)
            if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
                raise PermissionError("resource unavailable")

        async def guard() -> None:
            self._identity(identity)
            for request in requests:
                decision = await self._policy.evaluate(request, ctx)
                self._identity(identity)
                if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
                    raise _DeferredDenial(request, ctx, decision)

        return guard

    async def _write(self, operation: Awaitable[T]) -> T:
        try:
            return await operation
        except _DeferredDenial as exc:
            # The repository has rolled back before opening the normal audit writer.
            await self._policy.audit_decision(exc.request, exc.ctx, exc.decision)
            raise PermissionError("resource unavailable") from None

    async def create_session(self, identity: IdentityBinding, request_id: str) -> SessionView:
        self._identity(identity)
        value = JobSession(
            tenant_id=identity.actor.tenant_id,
            session_id=f"session_{uuid.uuid4().hex}",
            requester_subject_id=identity.actor.subject_id,
            requester_issuer=identity.issuer,
            requester_external_subject=identity.subject,
            created_at=self._clock(),
        )
        ctx = self._context(identity, request_id, value)
        guard = await self._authorize(identity, ctx, (self._request("session:create", ctx, value),))
        result = await self._write(self._repository.create_session(value, guard))
        self._identity(identity)
        return SessionView(session_id=result.session_id, created_at=result.created_at)

    async def submit(
        self,
        body: JobSubmission,
        key: str,
        identity: IdentityBinding,
        request_id: str,
    ) -> JobView:
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
            raise ValueError("invalid idempotency key")
        self._identity(identity)
        owner = await self._repository.get_session(identity.actor.tenant_id, body.session_id)
        self._identity(identity)
        self._owned(owner, identity)
        assert owner is not None
        now = self._clock()
        value = Job(
            tenant_id=owner.tenant_id,
            job_id=f"job_{uuid.uuid4().hex}",
            session_id=owner.session_id,
            thread_id=f"thread_{uuid.uuid4().hex}",
            run_id=f"run_{uuid.uuid4().hex}",
            requester_subject_id=owner.requester_subject_id,
            requester_issuer=owner.requester_issuer,
            requester_external_subject=owner.requester_external_subject,
            submission=body,
            request_hash=submission_hash(body),
            idempotency_key=hashlib.sha256(key.encode()).hexdigest(),
            timeout_at=now + timedelta(days=1),
            created_at=now,
            updated_at=now,
        )
        ctx = self._context(identity, request_id, value)
        requests = (
            self._request("session:read", ctx, owner),
            self._request("job:submit", ctx, owner),
            self._request(
                "campaign:draft:write" if body.spec.scenario == "campaign" else "analytics:read",
                ctx,
                value,
            ),
        )
        guard = await self._authorize(identity, ctx, requests)
        result = await self._write(self._repository.submit(value, guard))
        self._owned(result, identity)
        self._identity(identity)
        return job_view(result)

    async def _read(
        self,
        job_id: str,
        identity: IdentityBinding,
        request_id: str,
        action: str,
    ) -> tuple[Job, Guard]:
        self._identity(identity)
        value = await self._repository.get(identity.actor.tenant_id, job_id)
        self._identity(identity)
        self._owned(value, identity)
        assert value is not None
        ctx = self._context(identity, request_id, value)
        guard = await self._authorize(identity, ctx, (self._request(action, ctx, value),))
        return value, guard

    async def detail(self, job_id: str, identity: IdentityBinding, request_id: str) -> JobView:
        value, guard = await self._read(job_id, identity, request_id, "job:read")
        await self._write(guard())
        return job_view(value)

    async def cancel(self, job_id: str, identity: IdentityBinding, request_id: str) -> JobView:
        value, guard = await self._read(job_id, identity, request_id, "job:cancel")
        await self._write(guard())
        if value.status in {"cancelled", "cancellation_requested"}:
            return job_view(value)
        if value.status in TERMINAL_JOB_STATUSES:
            raise JobConflict("job has already finished")
        status = "cancellation_requested" if value.status == "running" else "cancelled"
        replacement = Job.model_validate(
            value.model_dump()
            | {
                "status": status,
                "cancel_requested": True,
                "revision": value.revision + 1,
                "updated_at": self._clock(),
                "retry_at": None,
            }
        )
        result = await self._write(
            self._repository.replace(
                value,
                replacement,
                guard,
                idempotent_cancel=True,
            )
        )
        self._identity(identity)
        return job_view(result)
