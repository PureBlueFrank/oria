"""Fixed coupon dispatch ledger, facts and events inside caller-owned transactions."""

import json
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import canonical_args_hash
from oria.core.context import Context
from oria.core.execution_ledger import ExecutionLedger
from oria.core.jobs import Job
from oria.core.types import EventEnvelope, JsonValue, ResourceRef
from oria.domain.launch import MATERIALIZE_TOOL_NAME, MaterializeCouponBatchArgs
from oria.domain.ledger import DomainEvent, LaunchPlan, OutboxRecord, Receipt, ToolExecution
from oria.storage.job_execution import JobCouponReservationWriter
from oria.storage.ledger import (
    SQLiteBusinessAuditRepository,
    SQLiteDomainEventRepository,
    SQLiteOutboxRepository,
)
from oria.storage.repositories import SQLiteCampaignLaunchRepository


class JobCouponDispatchWriter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._facts = JobCouponReservationWriter(sessions)
        self._tools = self._facts._tools
        self._launches = SQLiteCampaignLaunchRepository(sessions)
        self._events = SQLiteDomainEventRepository(sessions)
        self._audit = SQLiteBusinessAuditRepository(sessions)
        self._outbox = SQLiteOutboxRepository(sessions)

    async def load(
        self, session: AsyncSession, job: Job, args: MaterializeCouponBatchArgs, plan: LaunchPlan
    ) -> ToolExecution:
        locked = (
            await session.execute(
                text(
                    "UPDATE campaigns SET version = version WHERE tenant_id = :tenant "
                    "AND campaign_id = :campaign RETURNING campaign_id"
                ),
                {"tenant": job.tenant_id, "campaign": args.campaign_id},
            )
        ).scalar_one_or_none()
        if locked is None:
            raise PermissionError("persisted Job campaign is unavailable")
        execution = await self._tools._find_by_id(
            session, job.tenant_id, f"{job.job_id}:coupon-materialization:v1"
        )
        args_hash = canonical_args_hash(
            tool_name=MATERIALIZE_TOOL_NAME,
            tool_schema_version=1,
            schema=MaterializeCouponBatchArgs,
            args=args.model_dump(),
        )
        expected = self._facts.launch_saga(job, args)
        if execution is None or (
            execution.tool_name != MATERIALIZE_TOOL_NAME
            or execution.canonical_args_hash != args_hash
            or execution.idempotency_key
            != ExecutionLedger.build_idempotency_key(
                f"{args.campaign_id}:{args.coupon_spec_hash}", args_hash
            )
            or execution.checkpoint_id != expected.checkpoint
        ):
            raise PermissionError("a matching committed Job coupon reservation is required")
        await self._facts.validate_draft(
            session, job, args, plan, coupon_ready=execution.status == "succeeded"
        )
        saga = await self._facts._sagas._find_by_unique_key(
            session, (job.tenant_id, args.campaign_id), job.tenant_id
        )
        if execution.status == "succeeded":
            if execution.receipt_summary_hash is None:
                raise PermissionError("completed Job coupon receipt is unavailable")
            expected = expected.transition_to(
                "coupon_materialized", updated_at=execution.updated_at
            )
            coupon = await self._facts._drafts._coupons._find_by_id(
                session, args.coupon_batch_id, job.tenant_id
            )
            if coupon is None or coupon.updated_at != execution.updated_at:
                raise PermissionError("coupon receipt projection no longer matches history")
        if saga != expected:
            raise PermissionError("Job launch saga no longer matches coupon execution")
        return execution

    async def admit(
        self,
        session: AsyncSession,
        execution: ToolExecution,
        *,
        ctx: Context,
        epoch: int,
        policy_version: str,
        now: datetime,
    ) -> ToolExecution:
        result = await self._tools._transition(
            session,
            tenant_id=ctx.tenant_id,
            execution_id=execution.execution_id,
            target="executing",
            expected_status="reserved",
            updated_at=now,
        )
        await self._append(session, result, ctx, epoch, policy_version, "admitted", now)
        return result

    async def finish(
        self,
        session: AsyncSession,
        execution: ToolExecution,
        args: MaterializeCouponBatchArgs,
        receipt: Receipt | None,
        *,
        ctx: Context,
        epoch: int,
        policy_version: str,
        now: datetime,
    ) -> ToolExecution:
        target: Literal["succeeded", "failed", "unknown"] = "unknown"
        if receipt is not None:
            if receipt.status == "accepted":
                target = "succeeded"
            elif receipt.status == "rejected":
                target = "failed"
        if target == "succeeded":
            await self._launches.mark_coupon_ready(
                session,
                tenant_id=ctx.tenant_id,
                coupon_batch_id=args.coupon_batch_id,
                updated_at=now,
            )
            saga = await self._facts._sagas._find_by_unique_key(
                session, (ctx.tenant_id, args.campaign_id), ctx.tenant_id
            )
            if saga is None:
                raise PermissionError("Job launch saga is unavailable")
            await self._facts._sagas._update(
                session,
                saga,
                saga.transition_to("coupon_materialized", updated_at=now),
                allow_status_change=True,
            )
        result = await self._tools._transition(
            session,
            tenant_id=ctx.tenant_id,
            execution_id=execution.execution_id,
            target=target,
            expected_status="executing",
            updated_at=now,
            receipt_id=None if receipt is None else receipt.receipt_id,
            receipt_summary_hash=None if receipt is None else receipt.summary_hash,
            compensation_status="reconciliation_required" if target == "unknown" else None,
        )
        await self._append(session, result, ctx, epoch, policy_version, target, now)
        return result

    async def _append(
        self,
        session: AsyncSession,
        execution: ToolExecution,
        ctx: Context,
        epoch: int,
        policy_version: str,
        outcome: str,
        now: datetime,
    ) -> None:
        event_id = f"{execution.execution_id}:{outcome}"
        event_type = f"coupon_batch.job_dispatch.{outcome}"
        payload: dict[str, JsonValue] = {
            "job_id": ctx.job_id,
            "execution_id": execution.execution_id,
            "lease_epoch": epoch,
            "args_hash": execution.canonical_args_hash,
            "outcome": outcome,
        }
        resource = ResourceRef(
            tenant_id=ctx.tenant_id,
            resource_type="tool_execution",
            resource_id=execution.execution_id,
        )
        await self._events._append(
            session,
            DomainEvent(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                aggregate_type=resource.resource_type,
                aggregate_id=resource.resource_id,
                event_type=event_type,
                event_version=1,
                payload=payload,
                occurred_at=now,
                correlation_id=ctx.correlation_id,
            ),
        )
        await self._audit._append(
            session,
            EventEnvelope(
                event_id=f"{event_id}:audit",
                tenant_id=ctx.tenant_id,
                occurred_at=now,
                actor=ctx.actor.subject_id,
                action=event_type,
                resource=resource,
                decision="allow",
                policy_version=policy_version,
                args_hash=execution.canonical_args_hash,
                result="success" if outcome in {"admitted", "succeeded"} else "failure",
                correlation_id=ctx.correlation_id,
                payload=payload,
            ),
        )
        await self._outbox._append(
            session,
            OutboxRecord(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                topic=event_type,
                payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=now,
                available_at=now,
            ),
        )
