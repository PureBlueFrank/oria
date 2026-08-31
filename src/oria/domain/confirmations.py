"""Rule-driven business confirmation chain generation and timeout handling."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import Field

from oria.core.execution_ledger import ExecutionEventBundle, ExecutionLedger
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    EventEnvelope,
    JsonValue,
    ResourceRef,
    ValueModel,
)
from oria.domain.business import ConfirmationTask, EnrollmentItem
from oria.domain.ledger import DomainEvent, OutboxRecord, ToolExecution
from oria.domain.repositories import EnrollmentWorkflowRepository
from oria.rag.models import CampaignRuleSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oria.core.context import Context

ConfirmationSubjectType: TypeAlias = Literal["merchant", "sales", "sales_manager"]
TimeoutAction: TypeAlias = Literal["reject", "escalate", "explicit_auto_confirm"]
ConfirmationDecision: TypeAlias = Literal["confirm", "reject"]


class ConfirmationResolution(ValueModel):
    confirmation_task: ConfirmationTask
    enrollment_item: EnrollmentItem
    confirmation_tasks: tuple[ConfirmationTask, ...]
    next_confirmation_task: ConfirmationTask | None = None
    execution_id: str
    idempotency_key: str


class BusinessConfirmationPolicy(ValueModel):
    """Confirmation rules copied from one immutable campaign-rule snapshot."""

    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ordered_steps: tuple[ConfirmationSubjectType, ...]
    timeout_action: TimeoutAction

    @classmethod
    def from_snapshot(cls, snapshot: CampaignRuleSnapshot) -> BusinessConfirmationPolicy:
        if snapshot.recompute_hash() != snapshot.snapshot_hash:
            raise ValueError("campaign rule snapshot integrity verification failed")
        rule = snapshot.confirmation_policy
        return cls(
            rule_snapshot_id=snapshot.snapshot_id,
            rule_snapshot_hash=snapshot.snapshot_hash,
            ordered_steps=rule.ordered_steps,
            timeout_action=rule.timeout_action,
        )

    def generate_tasks(
        self,
        *,
        enrollment_item: EnrollmentItem,
        subject_ids: Mapping[ConfirmationSubjectType, str],
        created_at: datetime,
        due_at: datetime,
    ) -> tuple[ConfirmationTask, ...]:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        tasks: list[ConfirmationTask] = []
        for sequence, subject_type in enumerate(self.ordered_steps, start=1):
            subject_id = subject_ids.get(subject_type)
            if not subject_id:
                raise ValueError(f"confirmation subject is missing for {subject_type}")
            identity = (
                f"{self.rule_snapshot_hash}:{enrollment_item.tenant_id}:"
                f"{enrollment_item.enrollment_item_id}:{sequence}:{subject_type}"
            )
            task_id = "confirmation_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            tasks.append(
                ConfirmationTask(
                    tenant_id=enrollment_item.tenant_id,
                    confirmation_task_id=task_id,
                    version=1,
                    created_at=created_at,
                    updated_at=created_at,
                    enrollment_item_id=enrollment_item.enrollment_item_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    sequence=sequence,
                    due_at=due_at,
                    timeout_action=self.timeout_action,
                    status="pending" if sequence == 1 else "waiting",
                )
            )
        return tuple(tasks)

    def resolve_timeout(
        self,
        task: ConfirmationTask,
        *,
        updated_at: datetime,
        auto_confirm_authorized: bool = False,
    ) -> ConfirmationTask:
        if task.timeout_action != self.timeout_action:
            raise ValueError("confirmation task timeout action does not match frozen policy")
        if task.status != "pending":
            raise ValueError("only pending confirmation tasks can time out")
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        if updated_at < task.updated_at:
            raise ValueError("updated_at must not move backwards")
        if self.timeout_action == "explicit_auto_confirm":
            if not auto_confirm_authorized:
                raise PermissionError("explicit auto confirmation requires policy authorization")
            status = "confirmed"
        elif self.timeout_action == "reject":
            status = "rejected"
        else:
            status = "timed_out"
        return task.model_copy(
            update={"version": task.version + 1, "updated_at": updated_at, "status": status}
        )


class _ConfirmationCanonicalArgs(ValueModel):
    confirmation_task_id: str = Field(min_length=1)
    task_version: int = Field(ge=1)
    operation: Literal["confirm", "reject", "timeout"]


class ConfirmationService:
    """Advance one frozen confirmation chain in a single Business transaction."""

    def __init__(
        self,
        *,
        repository: EnrollmentWorkflowRepository,
        ledger: ExecutionLedger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._clock = clock

    async def decide(
        self,
        confirmation_task_id: str,
        decision: ConfirmationDecision,
        ctx: Context,
    ) -> ConfirmationResolution:
        item, tasks = await self._repository.load_confirmation_chain(
            tenant_id=ctx.tenant_id,
            confirmation_task_id=confirmation_task_id,
        )
        task = self._current_task(confirmation_task_id, item, tasks)
        policy_version = await self._authorize(
            action=f"confirmation:{task.subject_type}:decide",
            task=task,
            ctx=ctx,
        )
        now = self._now()
        if now >= task.due_at:
            raise ValueError("confirmation task is overdue")
        updated_item, updated_tasks = self._transition(
            item,
            tasks,
            task,
            outcome="confirmed" if decision == "confirm" else "rejected",
            updated_at=now,
        )
        return await self._commit(
            item=item,
            tasks=tasks,
            task=task,
            updated_item=updated_item,
            updated_tasks=updated_tasks,
            operation=decision,
            event_type="confirmation.decided",
            policy_version=policy_version,
            ctx=ctx,
        )

    async def resolve_timeout(
        self,
        confirmation_task_id: str,
        ctx: Context,
    ) -> ConfirmationResolution:
        item, tasks = await self._repository.load_confirmation_chain(
            tenant_id=ctx.tenant_id,
            confirmation_task_id=confirmation_task_id,
        )
        task = self._current_task(confirmation_task_id, item, tasks)
        now = self._now()
        if now < task.due_at:
            raise ValueError("confirmation task is not due")
        action = (
            "confirmation:timeout:auto_confirm"
            if task.timeout_action == "explicit_auto_confirm"
            else "confirmation:timeout:resolve"
        )
        policy_version = await self._authorize(action=action, task=task, ctx=ctx)
        outcome: Literal["confirmed", "rejected", "timed_out"]
        if task.timeout_action == "explicit_auto_confirm":
            outcome = "confirmed"
        elif task.timeout_action == "reject":
            outcome = "rejected"
        else:
            outcome = "timed_out"
        updated_item, updated_tasks = self._transition(
            item,
            tasks,
            task,
            outcome=outcome,
            updated_at=now,
        )
        return await self._commit(
            item=item,
            tasks=tasks,
            task=task,
            updated_item=updated_item,
            updated_tasks=updated_tasks,
            operation="timeout",
            event_type="confirmation.timeout_resolved",
            policy_version=policy_version,
            ctx=ctx,
        )

    @staticmethod
    def _current_task(
        confirmation_task_id: str,
        item: EnrollmentItem,
        tasks: tuple[ConfirmationTask, ...],
    ) -> ConfirmationTask:
        if item.status != "pending_confirmation":
            raise ValueError("enrollment item confirmation is already resolved")
        requested = next(
            (task for task in tasks if task.confirmation_task_id == confirmation_task_id), None
        )
        if requested is None:
            raise LookupError("confirmation task is unavailable")
        pending = tuple(task for task in tasks if task.status == "pending")
        if len(pending) != 1 or requested != pending[0]:
            if requested.status == "waiting":
                raise ValueError("confirmation task sequence is not active")
            raise ValueError("confirmation task is already resolved")
        return requested

    @staticmethod
    def _transition(
        item: EnrollmentItem,
        tasks: tuple[ConfirmationTask, ...],
        task: ConfirmationTask,
        *,
        outcome: Literal["confirmed", "rejected", "timed_out"],
        updated_at: datetime,
    ) -> tuple[EnrollmentItem, tuple[ConfirmationTask, ...]]:
        changed: dict[str, ConfirmationTask] = {
            task.confirmation_task_id: task._next_version(
                updated_at=updated_at,
                status=outcome,
            )
        }
        later = tuple(candidate for candidate in tasks if candidate.sequence > task.sequence)
        terminal_rejection = outcome == "rejected" or (outcome == "timed_out" and not later)
        if terminal_rejection:
            for candidate in later:
                if candidate.status == "waiting":
                    changed[candidate.confirmation_task_id] = candidate._next_version(
                        updated_at=updated_at,
                        status="cancelled",
                    )
            updated_item = item._next_version(updated_at=updated_at, status="rejected")
        elif later:
            next_task = min(later, key=lambda candidate: candidate.sequence)
            if next_task.status != "waiting":
                raise ValueError("confirmation chain next task is not waiting")
            ttl = next_task.due_at - next_task.created_at
            changed[next_task.confirmation_task_id] = next_task._next_version(
                updated_at=updated_at,
                due_at=updated_at + ttl,
                status="pending",
            )
            updated_item = item
        else:
            updated_item = item._next_version(updated_at=updated_at, status="confirmed")
        return updated_item, tuple(
            changed.get(candidate.confirmation_task_id, candidate) for candidate in tasks
        )

    async def _commit(
        self,
        *,
        item: EnrollmentItem,
        tasks: tuple[ConfirmationTask, ...],
        task: ConfirmationTask,
        updated_item: EnrollmentItem,
        updated_tasks: tuple[ConfirmationTask, ...],
        operation: Literal["confirm", "reject", "timeout"],
        event_type: str,
        policy_version: str,
        ctx: Context,
    ) -> ConfirmationResolution:
        canonical = _ConfirmationCanonicalArgs(
            confirmation_task_id=task.confirmation_task_id,
            task_version=task.version,
            operation=operation,
        )
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name=(
                "resolve_confirmation_timeout" if operation == "timeout" else "decide_confirmation"
            ),
            tool_schema_version=1,
            schema=_ConfirmationCanonicalArgs,
            args=canonical.model_dump(),
            stable_business_id=f"confirmation:{task.confirmation_task_id}:{task.version}",
            checkpoint_id=ctx.run_id,
        )
        if execution.status != "reserved":
            raise ValueError("confirmation task decision was already recorded")

        async def write(session: AsyncSession) -> None:
            await self._repository.apply_confirmation_chain(
                session,
                tenant_id=ctx.tenant_id,
                expected_item=item,
                expected_tasks=tasks,
                updated_item=updated_item,
                updated_tasks=updated_tasks,
            )

        events = self._events(
            execution=execution,
            item=updated_item,
            task=next(
                candidate
                for candidate in updated_tasks
                if candidate.confirmation_task_id == task.confirmation_task_id
            ),
            event_type=event_type,
            policy_version=policy_version,
            ctx=ctx,
        )
        succeeded = await self._ledger.record_local_success(
            execution,
            receipt_id=f"business:{task.confirmation_task_id}:v{task.version + 1}",
            business_write=write,
            domain_events=events.domain_events,
            audit_events=events.audit_events,
            outbox_records=events.outbox_records,
        )
        resolved_task = next(
            candidate
            for candidate in updated_tasks
            if candidate.confirmation_task_id == task.confirmation_task_id
        )
        next_task = next(
            (candidate for candidate in updated_tasks if candidate.status == "pending"), None
        )
        return ConfirmationResolution(
            confirmation_task=resolved_task,
            enrollment_item=updated_item,
            confirmation_tasks=updated_tasks,
            next_confirmation_task=next_task,
            execution_id=succeeded.execution_id,
            idempotency_key=succeeded.idempotency_key,
        )

    async def _authorize(
        self,
        *,
        action: str,
        task: ConfirmationTask,
        ctx: Context,
    ) -> str:
        decision = await ctx.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=action,
                resource=ResourceRef(
                    resource_type="confirmation_task",
                    resource_id=task.confirmation_task_id,
                    tenant_id=ctx.tenant_id,
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            ),
            ctx,
        )
        if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
            raise PermissionError("confirmation task decision is not authorized")
        return decision.policy_version

    def _events(
        self,
        *,
        execution: ToolExecution,
        item: EnrollmentItem,
        task: ConfirmationTask,
        event_type: str,
        policy_version: str,
        ctx: Context,
    ) -> ExecutionEventBundle:
        now = self._now()
        event_id = f"domain_event_{uuid.uuid4().hex}"
        payload: dict[str, JsonValue] = {
            "confirmation_task_id": task.confirmation_task_id,
            "confirmation_task_status": task.status,
            "enrollment_item_id": item.enrollment_item_id,
            "enrollment_item_status": item.status,
            "execution_id": execution.execution_id,
        }
        return ExecutionEventBundle(
            domain_events=(
                DomainEvent(
                    event_id=event_id,
                    tenant_id=ctx.tenant_id,
                    aggregate_type="enrollment_item",
                    aggregate_id=item.enrollment_item_id,
                    event_type=event_type,
                    event_version=task.version,
                    payload=payload,
                    occurred_at=now,
                    correlation_id=ctx.correlation_id,
                ),
            ),
            audit_events=(
                EventEnvelope(
                    event_id=f"business_audit_{uuid.uuid4().hex}",
                    occurred_at=now,
                    tenant_id=ctx.tenant_id,
                    actor=ctx.actor.subject_id,
                    action=execution.tool_name,
                    resource=ResourceRef(
                        resource_type="confirmation_task",
                        resource_id=task.confirmation_task_id,
                        tenant_id=ctx.tenant_id,
                    ),
                    decision="allow",
                    policy_version=policy_version,
                    args_hash=execution.canonical_args_hash,
                    result="success",
                    correlation_id=ctx.correlation_id,
                    payload={"execution_id": execution.execution_id, "status": task.status},
                ),
            ),
            outbox_records=(
                OutboxRecord(
                    event_id=event_id,
                    tenant_id=ctx.tenant_id,
                    topic=event_type,
                    payload_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    occurred_at=now,
                    available_at=now,
                ),
            ),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("confirmation service clock must return a timezone-aware time")
        return value
