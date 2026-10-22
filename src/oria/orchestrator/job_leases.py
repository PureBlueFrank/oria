"""Tokenless requester recovery and fenced Job-row operations for trusted workers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar

from oria.api.identity import AuthenticationError, IdentityDirectory
from oria.core.context import Context, RuntimeServices
from oria.core.job_leases import JobLeaseToken, LeaseOperation, validate_claim, validate_lease
from oria.core.jobs import Job, JobConflict
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    PolicyDecision,
    Principal,
    ResourceRef,
)
from oria.orchestrator.checkpoint import TenantCheckpointSaver, checkpoint_config
from oria.permission.local import LocalPolicyEngine
from oria.storage.jobs import Guard, JobRepository

T = TypeVar("T")
CompletionStatus = Literal["succeeded", "failed", "cancelled", "waiting_approval", "waiting_event"]


class _DeferredDenial(PermissionError):
    def __init__(self, request: AuthorizationRequest, ctx: Context, decision: PolicyDecision):
        super().__init__("job execution unavailable")
        self.request, self.ctx, self.decision = request, ctx, decision


class JobLeaseService:
    """Internal primitives only: no queue loop, Graph execution or external effects.

    The worker executor is explicitly trusted by Policy, independently of the API
    executor in the identity directory. A lease token alone grants no authority.
    """

    def __init__(
        self,
        runtime: RuntimeServices,
        repository: JobRepository,
        directory: Callable[[], IdentityDirectory],
        *,
        executor: Principal,
        instance_id: str | None = None,
        lease_seconds: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        checkpointer: TenantCheckpointSaver | None = None,
    ) -> None:
        if not isinstance(runtime.policy, LocalPolicyEngine):
            raise ValueError("Job leases currently require local Policy")
        executor = Principal.model_validate(executor.model_dump())
        if executor.kind != "service" or "job_worker" not in executor.roles:
            raise ValueError("an independent job_worker executor is required")
        instance = uuid.uuid4().hex if instance_id is None else instance_id
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", instance) is None:
            raise ValueError("invalid worker instance identifier")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("lease duration must be 1-300 seconds")
        self._runtime, self._repository, self._directory = runtime, repository, directory
        self._policy = runtime.policy
        self._executor, self._clock = executor, clock
        self._duration, self._saver = timedelta(seconds=lease_seconds), checkpointer
        self._owner = (
            "worker_"
            + hashlib.sha256(
                json.dumps([executor.tenant_id, executor.subject_id, instance]).encode()
            ).hexdigest()
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("worker clock must include timezone")
        return now.astimezone(UTC)

    def _actor(self, job: Job) -> Principal:
        binding = self._directory().resolve(job.requester_issuer, job.requester_external_subject)
        actor = binding.actor
        if binding.executor.subject_id == self._executor.subject_id:
            raise AuthenticationError("worker and API executors must be distinct")
        if (actor.tenant_id, actor.subject_id) != (job.tenant_id, job.requester_subject_id):
            raise AuthenticationError("requester binding changed")
        if (
            actor.tenant_id != self._executor.tenant_id
            or actor.subject_id == self._executor.subject_id
        ):
            raise AuthenticationError("invalid worker identity scope")
        return actor

    def _current(self, job: Job, ctx: Context) -> None:
        if self._actor(job) != ctx.actor:
            raise AuthenticationError("requester attributes changed")

    async def _load(self, job_id: str) -> Job:
        job = await self._repository.get(self._executor.tenant_id, job_id)
        if job is None:
            raise PermissionError("job unavailable")
        return job

    async def _authorize(self, job: Job) -> tuple[Context, Guard]:
        ctx = self._runtime.new_context(
            actor=self._actor(job),
            executor=self._executor,
            session_id=job.session_id,
            thread_id=job.thread_id,
            run_id=job.run_id,
            job_id=job.job_id,
            correlation_id=f"{job.job_id}:lease:{job.revision}",
        )
        business = (
            "campaign:draft:write"
            if job.submission.spec.scenario == "campaign"
            else "analytics:read"
        )
        requests = tuple(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=action,
                resource=ResourceRef(
                    tenant_id=job.tenant_id,
                    resource_type=resource_type,
                    resource_id=job.job_id,
                ),
                context=AuthorizationContext(
                    correlation_id=ctx.correlation_id,
                    attributes={
                        "requester_subject_id": job.requester_subject_id,
                        "resource_attributes": {},
                    },
                ),
            )
            for action, resource_type in (
                ("job:execute", "job"),
                (business, "campaign" if business == "campaign:draft:write" else "analytics_fact"),
            )
        )
        for request in requests:
            self._current(job, ctx)
            decision = await self._policy.authorize(request, ctx)
            self._current(job, ctx)
            if not decision.allow or decision.constraints.get("tenant_id") != job.tenant_id:
                raise PermissionError("job execution unavailable")

        async def guard() -> None:
            self._current(job, ctx)
            for request in requests:
                decision = await self._policy.evaluate(request, ctx)
                self._current(job, ctx)
                if not decision.allow or decision.constraints.get("tenant_id") != job.tenant_id:
                    raise _DeferredDenial(request, ctx, decision)

        return ctx, guard

    async def _write(self, operation: Awaitable[T]) -> T:
        try:
            return await operation
        except _DeferredDenial as exc:
            await self._policy.audit_decision(exc.request, exc.ctx, exc.decision)
            raise PermissionError("job execution unavailable") from None

    async def _update(
        self,
        expected: Job,
        replacement: Job,
        ctx: Context,
        guard: Guard,
        operation: LeaseOperation,
        token: JobLeaseToken | None,
    ) -> Job:
        result = await self._write(
            self._repository.replace_leased(
                expected,
                replacement,
                guard,
                operation=operation,
                token=token,
                clock=self._now,
            )
        )
        self._current(result, ctx)
        return result

    async def claim(self, job_id: str) -> Job:
        job = await self._load(job_id)
        ctx, guard = await self._authorize(job)
        now = self._now()
        validate_claim(job, now)
        replacement = Job.model_validate(
            job.model_dump()
            | {
                "status": "running",
                "revision": job.revision + 1,
                "attempt": job.attempt + 1 if job.status == "running" else max(1, job.attempt),
                "lease_epoch": job.lease_epoch + 1,
                "lease_owner": self._owner,
                "heartbeat_at": now,
                "lease_expires_at": min(now + self._duration, job.timeout_at),
                "updated_at": now,
            }
        )
        return await self._update(job, replacement, ctx, guard, "claim", None)

    async def _leased(self, token: JobLeaseToken) -> tuple[Job, Context, Guard]:
        token = JobLeaseToken.model_validate(token.model_dump())
        if token.tenant_id != self._executor.tenant_id or token.owner != self._owner:
            raise PermissionError("lease belongs to another worker")
        job = await self._load(token.job_id)
        validate_lease(job, token, self._now())
        ctx, guard = await self._authorize(job)
        return job, ctx, guard

    async def heartbeat(self, token: JobLeaseToken) -> Job:
        job, ctx, guard = await self._leased(token)
        now = self._now()
        replacement = Job.model_validate(
            job.model_dump()
            | {
                "revision": job.revision + 1,
                "updated_at": now,
                "heartbeat_at": now,
                "lease_expires_at": min(now + self._duration, job.timeout_at),
            }
        )
        return await self._update(job, replacement, ctx, guard, "heartbeat", token)

    async def finish(self, token: JobLeaseToken, status: CompletionStatus) -> Job:
        job, ctx, guard = await self._leased(token)
        replacement = Job.model_validate(
            job.model_dump()
            | {
                "status": status,
                "revision": job.revision + 1,
                "updated_at": self._now(),
                "lease_owner": None,
                "heartbeat_at": None,
                "lease_expires_at": None,
            }
        )
        return await self._update(job, replacement, ctx, guard, "finish", token)

    async def publish_checkpoint(self, token: JobLeaseToken, checkpoint_id: str) -> Job:
        job, ctx, guard = await self._leased(token)
        if job.status != "running":
            raise JobConflict("cancelled execution cannot publish a checkpoint")
        if self._saver is None:
            raise ValueError("checkpoint publication requires the tenant-scoped saver")
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", checkpoint_id) is None:
            raise ValueError("invalid checkpoint identifier")
        config = checkpoint_config(ctx)
        config["configurable"] |= {
            "checkpoint_ns": token.checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
        value = await self._saver.aget_tuple(config)
        await self._write(guard())
        validate_lease(job, token, self._now())
        if value is None:
            raise JobConflict("checkpoint is not available in the current epoch")
        if job.accepted_checkpoint_ns == token.checkpoint_ns:
            if job.accepted_checkpoint_id == checkpoint_id:
                return job
            # Only advance along the current accepted lineage, never to a sibling/ancestor.
            for _ in range(256):
                parent = value.parent_config
                if parent is None:
                    raise JobConflict("checkpoint does not descend from the accepted pointer")
                values = parent.get("configurable", {})
                if values.get("checkpoint_ns") != token.checkpoint_ns:
                    raise JobConflict("checkpoint does not descend from the accepted pointer")
                if values.get("checkpoint_id") == job.accepted_checkpoint_id:
                    break
                value = await self._saver.aget_tuple(parent)
                await self._write(guard())
                validate_lease(job, token, self._now())
                if value is None:
                    raise JobConflict("checkpoint lineage unavailable")
            else:
                raise JobConflict("checkpoint lineage exceeds the publication bound")
        replacement = Job.model_validate(
            job.model_dump()
            | {
                "revision": job.revision + 1,
                "updated_at": self._now(),
                "accepted_checkpoint_ns": token.checkpoint_ns,
                "accepted_checkpoint_id": checkpoint_id,
            }
        )
        return await self._update(job, replacement, ctx, guard, "checkpoint", token)
