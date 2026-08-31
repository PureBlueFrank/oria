"""Deterministic merchant/auto/hybrid enrollment branch and join semantics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from oria.core.approvals import (
    ApprovalBindingInvalidationFact,
    ApprovalInvalidationResult,
    approval_binding_event_id,
)
from oria.core.integration_events import (
    EnrollmentWindowClosed,
    ExternalWait,
    IntegrationEventInboxService,
    IntegrationInboxRecord,
    MerchantEnrollmentUpserted,
    parse_integration_event,
)
from oria.core.types import ValueModel
from oria.domain.business import EnrollmentMode
from oria.domain.enrollment import (
    AutoCircleRunBinding,
    AutoEnrollmentCommand,
    EnrollmentItemInput,
    EnrollmentService,
    MerchantEnrollmentCommand,
    UpsertEnrollmentItemsResult,
)
from oria.rag.models import CampaignRuleSnapshot

if TYPE_CHECKING:
    from oria.core.context import Context

LateEventAction = Literal["reject", "new_version"]
BranchEventStatus = Literal[
    "accepted",
    "duplicate",
    "ignored",
    "late_rejected",
    "new_version",
    "window_closed",
    "window_timeout",
    "auto_completed",
]


class EnrollmentBranchState(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    campaign_id: str = Field(min_length=1)
    mode: EnrollmentMode
    enrollment_window_start: datetime
    enrollment_window_end: datetime
    late_event_action: LateEventAction
    window_closed: bool = False
    auto_completed: bool = False
    enrollment_version: int = Field(default=1, ge=1)
    accepted_item_keys: tuple[str, ...] = ()
    late_rejected_event_ids: tuple[str, ...] = ()
    downstream_approval_invalidated: bool = False
    downstream_approval_invalidation_pending: bool = False

    @field_validator("enrollment_window_start", "enrollment_window_end")
    @classmethod
    def require_aware_window(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enrollment window must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.enrollment_window_start >= self.enrollment_window_end:
            raise ValueError("enrollment window must have a positive duration")
        return self

    @classmethod
    def from_snapshot(
        cls,
        *,
        campaign_id: str,
        snapshot: CampaignRuleSnapshot,
    ) -> EnrollmentBranchState:
        if snapshot.recompute_hash() != snapshot.snapshot_hash:
            raise ValueError("campaign rule snapshot integrity verification failed")
        start, end = _parse_interval(snapshot.basic.enrollment_window)
        return cls(
            tenant_id=snapshot.tenant_id,
            campaign_id=campaign_id,
            mode=snapshot.enrollment_policy.mode,
            enrollment_window_start=start,
            enrollment_window_end=end,
            late_event_action=snapshot.enrollment_policy.late_event_action,
        )

    @property
    def join_complete(self) -> bool:
        if self.mode == "merchant":
            return self.window_closed
        if self.mode == "auto":
            return self.auto_completed
        return self.window_closed and self.auto_completed


class EnrollmentBranchOutcome(ValueModel):
    status: BranchEventStatus
    state: EnrollmentBranchState
    write_result: UpsertEnrollmentItemsResult | None = None
    inbox_status: str | None = None


class DownstreamApprovalInvalidator(Protocol):
    async def consume(
        self,
        fact: ApprovalBindingInvalidationFact,
    ) -> ApprovalInvalidationResult: ...


class InMemoryDownstreamApprovalInvalidator:
    def __init__(self) -> None:
        self.invalidations: list[ApprovalBindingInvalidationFact] = []

    async def consume(
        self,
        fact: ApprovalBindingInvalidationFact,
    ) -> ApprovalInvalidationResult:
        if fact not in self.invalidations:
            self.invalidations.append(fact)
        return ApprovalInvalidationResult(
            event_id=fact.event_id,
            status="applied",
            invalidated_count=1,
        )


class EnrollmentBranchCoordinator:
    """Apply only inbox-classified events; inbound checkpoint/wait IDs are never read."""

    def __init__(
        self,
        *,
        inbox: IntegrationEventInboxService,
        enrollments: EnrollmentService,
        approval_invalidator: DownstreamApprovalInvalidator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inbox = inbox
        self._enrollments = enrollments
        self._approval_invalidator = approval_invalidator
        self._clock = clock

    async def process_event(
        self,
        state: EnrollmentBranchState,
        value: object,
        *,
        wait: ExternalWait | None,
        ctx: Context,
    ) -> EnrollmentBranchOutcome:
        inbox_result = await self._inbox.process(value, wait=wait)
        if inbox_result.status == "duplicate":
            return EnrollmentBranchOutcome(
                status="duplicate", state=state, inbox_status=inbox_result.status
            )
        if inbox_result.record is None:
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        event = parse_integration_event(value)
        if event.tenant_id != state.tenant_id or event.payload.campaign_id != state.campaign_id:
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        if inbox_result.status not in {"matched", "no_wait", "wait_expired"}:
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        if isinstance(event, EnrollmentWindowClosed):
            if not inbox_result.resume_eligible:
                return EnrollmentBranchOutcome(
                    status="ignored", state=state, inbox_status=inbox_result.status
                )
            return EnrollmentBranchOutcome(
                status="window_closed",
                state=state.model_copy(update={"window_closed": True}),
                inbox_status=inbox_result.status,
            )
        if not isinstance(event, MerchantEnrollmentUpserted):
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        now = self._now()
        late = state.window_closed or now >= state.enrollment_window_end
        if late:
            if state.late_event_action == "reject":
                rejected = state.model_copy(
                    update={
                        "late_rejected_event_ids": (
                            *state.late_rejected_event_ids,
                            event.source_event_id,
                        )
                    }
                )
                return EnrollmentBranchOutcome(
                    status="late_rejected",
                    state=rejected,
                    inbox_status=inbox_result.status,
                )
            result = await self._write_event(
                event,
                inbox_result.record,
                ctx,
                new_enrollment_version=True,
            )
            binding = result.approval_binding
            fact = ApprovalBindingInvalidationFact(
                event_id=approval_binding_event_id(state.tenant_id, binding),
                tenant_id=state.tenant_id,
                binding=binding,
                reason="late_enrollment_new_version",
                occurred_at=now,
            )
            try:
                invalidation = await self._approval_invalidator.consume(fact)
            except Exception:
                invalidation = ApprovalInvalidationResult(
                    event_id=fact.event_id,
                    status="reconciliation",
                    invalidated_count=0,
                )
            applied = invalidation.status == "applied"
            return EnrollmentBranchOutcome(
                status="new_version",
                state=self._accept_key(state, event).model_copy(
                    update={
                        "enrollment_version": state.enrollment_version + 1,
                        "downstream_approval_invalidated": applied,
                        "downstream_approval_invalidation_pending": not applied,
                    }
                ),
                write_result=result,
                inbox_status=inbox_result.status,
            )
        if now < state.enrollment_window_start or not inbox_result.resume_eligible:
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        if state.mode == "auto":
            return EnrollmentBranchOutcome(
                status="ignored", state=state, inbox_status=inbox_result.status
            )
        result = await self._write_event(
            event,
            inbox_result.record,
            ctx,
            new_enrollment_version=False,
        )
        return EnrollmentBranchOutcome(
            status="accepted",
            state=self._accept_key(state, event),
            write_result=result,
            inbox_status=inbox_result.status,
        )

    async def complete_auto(
        self,
        state: EnrollmentBranchState,
        items: tuple[EnrollmentItemInput, ...],
        *,
        binding: AutoCircleRunBinding,
        ctx: Context,
    ) -> EnrollmentBranchOutcome:
        if state.mode == "merchant":
            raise ValueError("merchant enrollment mode has no auto branch")
        result = await self._enrollments.upsert_auto(
            AutoEnrollmentCommand(
                campaign_id=state.campaign_id,
                items=items,
                binding=binding,
            ),
            ctx,
        )
        keys = tuple(
            sorted(
                set(state.accepted_item_keys)
                | {
                    _item_key(item.merchant_id, item.product_ref, item.product_version)
                    for item in items
                }
            )
        )
        return EnrollmentBranchOutcome(
            status="auto_completed",
            state=state.model_copy(update={"auto_completed": True, "accepted_item_keys": keys}),
            write_result=result,
        )

    def resolve_window_timeout(
        self,
        state: EnrollmentBranchState,
    ) -> EnrollmentBranchOutcome:
        if self._now() < state.enrollment_window_end:
            raise ValueError("enrollment window has not timed out")
        return EnrollmentBranchOutcome(
            status="window_timeout",
            state=state.model_copy(update={"window_closed": True}),
        )

    async def _write_event(
        self,
        event: MerchantEnrollmentUpserted,
        inbox_record: IntegrationInboxRecord,
        ctx: Context,
        *,
        new_enrollment_version: bool,
    ) -> UpsertEnrollmentItemsResult:
        return await self._enrollments.upsert_merchant(
            MerchantEnrollmentCommand(event=event, inbox_record=inbox_record),
            ctx,
            new_enrollment_version=new_enrollment_version,
        )

    @staticmethod
    def _accept_key(
        state: EnrollmentBranchState,
        event: MerchantEnrollmentUpserted,
    ) -> EnrollmentBranchState:
        payload = event.payload
        keys = tuple(
            sorted(
                set(state.accepted_item_keys)
                | {_item_key(payload.merchant_id, payload.product_ref, payload.product_version)}
            )
        )
        return state.model_copy(update={"accepted_item_keys": keys})

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enrollment branch clock must return a timezone-aware time")
        return value


def _parse_interval(value: str) -> tuple[datetime, datetime]:
    start_value, end_value = value.split("/", 1)
    return datetime.fromisoformat(start_value), datetime.fromisoformat(end_value)


def _item_key(merchant_id: str, product_ref: str, product_version: str) -> str:
    return f"{merchant_id}:{product_ref}:{product_version}"
