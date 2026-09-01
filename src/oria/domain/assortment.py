"""Post-enrollment assortment, consumer placement, and merchant notification services."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from oria.core.approvals import (
    Approval,
    ApprovalBindingInvalidationFact,
    ApprovalBusinessBinding,
    ApprovalResumeRequest,
    ApprovalService,
    canonical_args_hash,
)
from oria.core.execution_ledger import (
    BusinessMutation,
    ExecutionEventBundle,
    ExecutionLedger,
    ExecutionOutcome,
    OutcomeProjectionMutation,
    ProjectionOutcome,
)
from oria.core.integration_events import (
    ConsumedIntegrationInbox,
    IntegrationInboxIdentity,
    SelectionCompleted,
    SelectionDecisionRecorded,
    TrustedIntegrationEventInboxRepository,
    integration_payload_hash,
)
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    EventEnvelope,
    JsonValue,
    ResourceRef,
    RetryPolicy,
    ToolPolicy,
    ValueModel,
)
from oria.domain.business import (
    AssortmentSubmission,
    Campaign,
    ConsumerPlacement,
    EnrollmentCouponLink,
    EnrollmentItem,
    MerchantNotification,
    SelectionDecision,
)
from oria.domain.ledger import DomainEvent, OutboxRecord, Receipt, ToolExecution
from oria.domain.product_eligibility import ProductEligibilityCriteria
from oria.domain.repositories import CampaignRepository, CampaignRuleSnapshotRefRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oria.core.context import Context
    from oria.rag.models import CampaignRuleSnapshot

SUBMIT_ASSORTMENT_TOOL_NAME = "submit_assortment"
PUBLISH_CONSUMER_TOOL_NAME = "publish_consumer_placement"
SEND_NOTIFICATION_TOOL_NAME = "send_merchant_notification"

SUBMIT_ASSORTMENT_POLICY = ToolPolicy(
    risk_level="medium",
    side_effect=True,
    timeout_seconds=30,
    retry_policy=RetryPolicy(max_attempts=1),
    idempotency_scope="campaign_id:submission_version",
    required_action="assortment:submit",
    resource_type="campaign",
    approval_mode="conditional",
    approval_action="assortment_submission_approval",
)
PUBLISH_CONSUMER_POLICY = ToolPolicy(
    risk_level="high",
    side_effect=True,
    timeout_seconds=30,
    retry_policy=RetryPolicy(max_attempts=1),
    idempotency_scope="campaign_id:selection_version:placement_spec_hash",
    required_action="consumer:publish",
    resource_type="campaign",
    approval_mode="required",
    approval_action="consumer_publish_approval",
)
SEND_NOTIFICATION_POLICY = ToolPolicy(
    risk_level="medium",
    side_effect=True,
    timeout_seconds=30,
    retry_policy=RetryPolicy(max_attempts=1),
    idempotency_scope="merchant_id:campaign_id:result_version:template_id:channel",
    required_action="notification:send",
    resource_type="campaign",
    approval_mode="conditional",
    approval_action="merchant_notification_approval",
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def selection_result_hash(
    *,
    campaign_id: str,
    submission_version: str,
    selection_version: str,
    decisions: tuple[SelectionDecision, ...],
) -> str:
    """Hash the complete ordered selection result sealed at completion."""
    return _hash(
        {
            "campaign_id": campaign_id,
            "decisions": [
                {
                    "decision": decision.decision,
                    "enrollment_item_id": decision.enrollment_item_id,
                    "reason_code": decision.reason_code,
                }
                for decision in sorted(decisions, key=lambda item: item.enrollment_item_id)
            ],
            "selection_version": selection_version,
            "submission_version": submission_version,
        }
    )


class SubmitAssortmentArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    enrollment_item_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    assortment_policy_ref: str = Field(min_length=1)
    assortment_policy_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    approval_id: str | None = Field(default=None, min_length=1)

    @field_validator("enrollment_item_ids")
    @classmethod
    def require_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("enrollment_item_ids must be unique")
        return value


class AssortmentAdapterCapabilities(ValueModel):
    reversible: bool
    max_automatic_items: int = Field(ge=1)
    preapproved_policy_bindings: frozenset[str] = frozenset()

    def approval_reasons(self, args: SubmitAssortmentArgs) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.reversible:
            reasons.append("irreversible_adapter")
        if len(args.enrollment_item_ids) > self.max_automatic_items:
            reasons.append("broad_scope")
        binding = f"{args.assortment_policy_ref}@{args.assortment_policy_version}"
        if binding not in self.preapproved_policy_bindings:
            reasons.append("policy_upgrade")
        return tuple(reasons)


class AssortmentAdapter(Protocol):
    adapter_id: str
    capabilities: AssortmentAdapterCapabilities

    async def submit(
        self,
        args: SubmitAssortmentArgs,
        *,
        submission_version: str,
        idempotency_key: str,
    ) -> Receipt: ...


class SubmitAssortmentResult(ValueModel):
    schema_version: Literal[1] = 1
    submission: AssortmentSubmission
    enrollment_item_ids: tuple[str, ...]
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str
    replay_status: Literal["completed", "waiting", "reconciliation"] = "completed"


class SelectionEventResult(ValueModel):
    schema_version: Literal[1] = 1
    event_type: Literal["selection.decision_recorded", "selection.completed"]
    selection_version: str
    execution_id: str
    idempotency_key: str
    approval_binding: ApprovalBusinessBinding
    invalidation_status: Literal["not_required", "applied", "reconciliation"]


class PublishConsumerPlacementArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    selection_version: str = Field(min_length=1)
    placement_spec: dict[str, JsonValue] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    approval_id: str | None = Field(default=None, min_length=1)


class ConsumerPlacementAdapter(Protocol):
    adapter_id: str

    async def publish(
        self,
        args: PublishConsumerPlacementArgs,
        *,
        selected_item_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> Receipt: ...


class PublishConsumerPlacementResult(ValueModel):
    schema_version: Literal[1] = 1
    placement: ConsumerPlacement
    selected_item_ids: tuple[str, ...]
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str
    replay_status: Literal["completed", "waiting", "reconciliation"] = "completed"


class MerchantNotificationMessage(ValueModel):
    merchant_id: str = Field(min_length=1, repr=False)
    campaign_id: str = Field(min_length=1)
    result_version: str = Field(min_length=1)
    selected_item_ids: tuple[str, ...]
    rejected_reasons: tuple[str, ...]
    template_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)


class NotificationAdapterCapabilities(ValueModel):
    standard_template_ids: frozenset[str]
    sensitive_template_ids: frozenset[str] = frozenset()
    max_attempts: int = Field(default=3, ge=1, le=10)

    def approval_reasons(self, template_id: str) -> tuple[str, ...]:
        reasons: list[str] = []
        if template_id not in self.standard_template_ids:
            reasons.append("non_standard_template")
        if template_id in self.sensitive_template_ids:
            reasons.append("sensitive_content")
        return tuple(reasons)


class MerchantNotificationAdapter(Protocol):
    adapter_id: str
    capabilities: NotificationAdapterCapabilities

    async def send(
        self,
        message: MerchantNotificationMessage,
        *,
        idempotency_key: str,
        attempt: int,
    ) -> Receipt: ...


class SendMerchantNotificationArgs(ValueModel):
    merchant_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    result_version: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    approval_id: str | None = Field(default=None, min_length=1)


class SendMerchantNotificationResult(ValueModel):
    schema_version: Literal[1] = 1
    notification: MerchantNotification
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str
    replay_status: Literal["completed", "waiting", "reconciliation"] = "completed"


class AssortmentSelection(ValueModel):
    campaign: Campaign
    binding: ApprovalBusinessBinding
    submission: AssortmentSubmission
    enrollment_item_ids: tuple[str, ...]
    decisions: tuple[SelectionDecision, ...]
    items: tuple[EnrollmentItem, ...]
    links: tuple[EnrollmentCouponLink, ...]

    @property
    def selected_item_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                decision.enrollment_item_id
                for decision in self.decisions
                if decision.decision == "selected"
            )
        )


class AssortmentCandidateSet(ValueModel):
    """Server-derived frozen candidate projection used again in the commit transaction."""

    tenant_id: str = Field(min_length=1, repr=False)
    campaign_id: str = Field(min_length=1)
    rule_snapshot_ref_id: str = Field(min_length=1)
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    product_policy_ref: str = Field(min_length=1)
    product_policy_version: str = Field(min_length=1)
    assortment_policy_ref: str = Field(min_length=1)
    assortment_policy_version: str = Field(min_length=1)
    approval_binding: ApprovalBusinessBinding
    enrollment_item_ids: tuple[str, ...] = Field(min_length=1, repr=False)
    candidate_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        product_criteria: ProductEligibilityCriteria,
        assortment_policy_ref: str,
        assortment_policy_version: str,
        approval_binding: ApprovalBusinessBinding,
        enrollment_item_ids: tuple[str, ...],
    ) -> AssortmentCandidateSet:
        ordered_ids = tuple(sorted(set(enrollment_item_ids)))
        payload = cls._hash_payload(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            rule_snapshot_ref_id=rule_snapshot_ref_id,
            rule_snapshot_hash=product_criteria.rule_snapshot_hash,
            product_policy_ref=product_criteria.policy_ref,
            product_policy_version=product_criteria.policy_version,
            assortment_policy_ref=assortment_policy_ref,
            assortment_policy_version=assortment_policy_version,
            approval_binding=approval_binding,
            enrollment_item_ids=ordered_ids,
        )
        return cls(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            rule_snapshot_ref_id=rule_snapshot_ref_id,
            rule_snapshot_hash=product_criteria.rule_snapshot_hash,
            product_policy_ref=product_criteria.policy_ref,
            product_policy_version=product_criteria.policy_version,
            assortment_policy_ref=assortment_policy_ref,
            assortment_policy_version=assortment_policy_version,
            approval_binding=approval_binding,
            enrollment_item_ids=ordered_ids,
            candidate_set_hash=_hash(payload),
        )

    @staticmethod
    def _hash_payload(
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        rule_snapshot_hash: str,
        product_policy_ref: str,
        product_policy_version: str,
        assortment_policy_ref: str,
        assortment_policy_version: str,
        approval_binding: ApprovalBusinessBinding,
        enrollment_item_ids: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "approval_binding": approval_binding.model_dump(mode="json"),
            "assortment_policy_ref": assortment_policy_ref,
            "assortment_policy_version": assortment_policy_version,
            "campaign_id": campaign_id,
            "enrollment_item_ids": enrollment_item_ids,
            "product_policy_ref": product_policy_ref,
            "product_policy_version": product_policy_version,
            "rule_snapshot_hash": rule_snapshot_hash,
            "rule_snapshot_ref_id": rule_snapshot_ref_id,
            "tenant_id": tenant_id,
        }

    @model_validator(mode="after")
    def verify_candidate_hash(self) -> AssortmentCandidateSet:
        payload = self._hash_payload(
            tenant_id=self.tenant_id,
            campaign_id=self.campaign_id,
            rule_snapshot_ref_id=self.rule_snapshot_ref_id,
            rule_snapshot_hash=self.rule_snapshot_hash,
            product_policy_ref=self.product_policy_ref,
            product_policy_version=self.product_policy_version,
            assortment_policy_ref=self.assortment_policy_ref,
            assortment_policy_version=self.assortment_policy_version,
            approval_binding=self.approval_binding,
            enrollment_item_ids=self.enrollment_item_ids,
        )
        if self.enrollment_item_ids != tuple(sorted(set(self.enrollment_item_ids))):
            raise ValueError("assortment candidate IDs must be unique and ordered")
        if _hash(payload) != self.candidate_set_hash:
            raise ValueError("assortment candidate set hash does not match")
        return self


class AssortmentWorkflowRepository(Protocol):
    async def get_approval_binding(
        self, *, tenant_id: str, campaign_id: str
    ) -> ApprovalBusinessBinding | None: ...

    async def load_submission_candidates(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        product_criteria: ProductEligibilityCriteria,
        assortment_policy_ref: str,
        assortment_policy_version: str,
        approval_binding: ApprovalBusinessBinding,
    ) -> AssortmentCandidateSet: ...

    async def persist_submission_outcome(
        self,
        session: AsyncSession,
        *,
        submission: AssortmentSubmission,
        enrollment_item_ids: tuple[str, ...],
        candidate_set: AssortmentCandidateSet,
        product_criteria: ProductEligibilityCriteria,
        expected_campaign_version: int,
        outcome: ExecutionOutcome,
    ) -> None: ...

    def submission_outcome_projection(
        self,
        *,
        execution_id: str,
        submission: AssortmentSubmission,
    ) -> OutcomeProjectionMutation: ...

    async def load_submission(
        self, *, tenant_id: str, campaign_id: str, submission_version: str
    ) -> tuple[AssortmentSubmission, tuple[str, ...]]: ...

    async def record_selection_decision(
        self,
        session: AsyncSession,
        *,
        decision: SelectionDecision,
    ) -> None: ...

    async def complete_selection(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
        selection_version: str,
        expected_binding: ApprovalBusinessBinding,
        updated_binding: ApprovalBusinessBinding,
        updated_at: datetime,
    ) -> None: ...

    async def selection_completion_hash(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
        selection_version: str,
    ) -> str: ...

    async def load_selection(
        self, *, tenant_id: str, campaign_id: str, selection_version: str
    ) -> AssortmentSelection: ...

    async def persist_placement_outcome(
        self,
        session: AsyncSession,
        *,
        placement: ConsumerPlacement,
        expected_binding: ApprovalBusinessBinding,
        selected_item_ids: tuple[str, ...],
        outcome: ExecutionOutcome,
    ) -> None: ...

    def placement_outcome_projection(
        self,
        *,
        execution_id: str,
        placement: ConsumerPlacement,
    ) -> OutcomeProjectionMutation: ...

    async def load_placement(self, *, tenant_id: str, placement_id: str) -> ConsumerPlacement: ...

    async def notification_message(
        self,
        *,
        tenant_id: str,
        merchant_id: str,
        campaign_id: str,
        result_version: str,
        template_id: str,
        channel: str,
    ) -> MerchantNotificationMessage: ...

    async def persist_notification_outcome(
        self,
        session: AsyncSession,
        *,
        notification: MerchantNotification,
    ) -> None: ...

    def notification_outcome_projection(
        self,
        *,
        execution_id: str,
        notification: MerchantNotification,
        outcome: ProjectionOutcome,
    ) -> OutcomeProjectionMutation: ...

    async def load_notification(
        self, *, tenant_id: str, notification_id: str
    ) -> MerchantNotification: ...


class RuleSnapshotReader(Protocol):
    async def get(self, snapshot_id: str, ctx: Context) -> CampaignRuleSnapshot: ...


class DownstreamApprovalInvalidator(Protocol):
    async def consume(self, fact: ApprovalBindingInvalidationFact) -> object: ...


class TrustedSelectionEventService:
    """Authorize the integration executor and atomically consume a persisted inbox event."""

    def __init__(
        self,
        inbox: TrustedIntegrationEventInboxRepository,
        assortment: AssortmentService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inbox = inbox
        self._assortment = assortment
        self._clock = clock

    async def apply(
        self,
        event: SelectionDecisionRecorded | SelectionCompleted,
        ctx: Context,
    ) -> SelectionEventResult:
        policy_version = await self._assortment._authorize_selection_event(event, ctx)
        consumed = await self._inbox.consume_matched(
            IntegrationInboxIdentity(
                tenant_id=event.tenant_id,
                adapter_id=event.adapter_id,
                source_event_id=event.source_event_id,
            ),
            event,
            consumed_at=self._now(),
        )
        return await self._assortment._record_selection_event(
            event,
            consumed,
            policy_version=policy_version,
            ctx=ctx,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("selection inbox clock must return a timezone-aware timestamp")
        return now


class AssortmentService:
    """Execute T06 writes through exact policy, version, approval, and ledger bindings."""

    def __init__(
        self,
        *,
        campaigns: CampaignRepository,
        rule_refs: CampaignRuleSnapshotRefRepository,
        rule_snapshots: RuleSnapshotReader,
        repository: AssortmentWorkflowRepository,
        ledger: ExecutionLedger,
        approvals: ApprovalService,
        assortment_adapter: AssortmentAdapter,
        placement_adapter: ConsumerPlacementAdapter,
        notification_adapter: MerchantNotificationAdapter,
        approval_invalidator: DownstreamApprovalInvalidator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._campaigns = campaigns
        self._rule_refs = rule_refs
        self._rule_snapshots = rule_snapshots
        self._repository = repository
        self._ledger = ledger
        self._approvals = approvals
        self._assortment_adapter = assortment_adapter
        self._placement_adapter = placement_adapter
        self._notification_adapter = notification_adapter
        self._approval_invalidator = approval_invalidator
        self._clock = clock

    async def request_assortment_approval(
        self,
        request: SubmitAssortmentArgs,
        *,
        expires_at: datetime,
        ctx: Context,
    ) -> Approval:
        canonical, args_hash, binding, _, _, _ = await self._assortment_precheck(request, ctx)
        reasons = self._assortment_adapter.capabilities.approval_reasons(canonical)
        if not reasons:
            raise ValueError("assortment submission does not require conditional approval")
        return await self._approvals.create(
            approval_action="assortment_submission_approval",
            tool_name=SUBMIT_ASSORTMENT_TOOL_NAME,
            canonical_args_hash=args_hash,
            checkpoint_id=ctx.run_id,
            expires_at=expires_at,
            ctx=ctx,
            business_binding=binding,
        )

    async def submit(
        self,
        request: SubmitAssortmentArgs,
        ctx: Context,
    ) -> SubmitAssortmentResult:
        (
            canonical,
            args_hash,
            binding,
            candidate_set,
            campaign,
            product_criteria,
        ) = await self._assortment_precheck(request, ctx)
        ordered_ids = canonical.enrollment_item_ids
        submission_version = _stable_id(
            "submission_version",
            request.campaign_id,
            request.assortment_policy_ref,
            request.assortment_policy_version,
            *ordered_ids,
        )
        reasons = self._assortment_adapter.capabilities.approval_reasons(canonical)
        await self._approvals.authorize_resume(
            request=ApprovalResumeRequest(
                approval_id=request.approval_id,
                approval_action="assortment_submission_approval",
                tool_name=SUBMIT_ASSORTMENT_TOOL_NAME,
                canonical_args_hash=args_hash,
                checkpoint_id=ctx.run_id,
                approval_required=bool(reasons),
                business_binding=binding,
            ),
            tool_policy=SUBMIT_ASSORTMENT_POLICY,
            ctx=ctx,
        )
        policy_version = await _authorize("assortment:submit", request.campaign_id, ctx)
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name=SUBMIT_ASSORTMENT_TOOL_NAME,
            tool_schema_version=1,
            schema=SubmitAssortmentArgs,
            args=canonical.model_dump(),
            stable_business_id=f"{request.campaign_id}:{submission_version}",
            checkpoint_id=ctx.run_id,
            request_idempotency_key=request.idempotency_key,
        )
        if args_hash != execution.canonical_args_hash:
            raise RuntimeError("assortment canonical binding changed during execution")
        now = self._now()

        def submission_for(outcome: ExecutionOutcome) -> AssortmentSubmission:
            status = {"succeeded": "submitted", "failed": "failed", "unknown": "unknown"}[outcome]
            return AssortmentSubmission(
                tenant_id=ctx.tenant_id,
                assortment_submission_id=_stable_id(
                    "assortment_submission", ctx.tenant_id, request.campaign_id, submission_version
                ),
                campaign_id=request.campaign_id,
                submission_version=submission_version,
                assortment_policy_ref=request.assortment_policy_ref,
                assortment_policy_version=request.assortment_policy_version,
                status=status,  # type: ignore[arg-type]
                version=1,
                created_at=now,
                updated_at=now,
            )

        def success_write() -> BusinessMutation:
            submission = submission_for("succeeded")

            async def write(session: AsyncSession) -> None:
                await self._repository.persist_submission_outcome(
                    session,
                    submission=submission,
                    enrollment_item_ids=ordered_ids,
                    candidate_set=candidate_set,
                    product_criteria=product_criteria,
                    expected_campaign_version=campaign.version,
                    outcome="succeeded",
                )

            return write

        def projection_for(outcome: ProjectionOutcome) -> OutcomeProjectionMutation:
            return self._repository.submission_outcome_projection(
                execution_id=execution.execution_id,
                submission=submission_for(outcome),
            )

        def events_for(outcome: ExecutionOutcome) -> ExecutionEventBundle:
            return _execution_events(
                now=now,
                execution=execution,
                aggregate_type="assortment_submission",
                aggregate_id=submission_version,
                event_type=f"assortment.submission_{outcome}",
                count=len(ordered_ids),
                outcome=outcome,
                policy_version=policy_version,
                ctx=ctx,
            )

        if execution.status == "executing":
            events = events_for("unknown")
            execution = await self._ledger.recover_stale_executing(
                execution,
                outcome_projection=projection_for("unknown"),
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
            if execution.status == "executing":
                return self._waiting_submission_result(
                    request,
                    execution,
                    submission_version,
                    ordered_ids,
                    now,
                )
        if execution.status in {"succeeded", "failed", "unknown"}:
            return await self._submission_result(request, execution, submission_version)
        if execution.status != "reserved":
            raise RuntimeError("assortment execution state is not replayable")

        async def invoke(idempotency_key: str) -> Receipt:
            return await self._assortment_adapter.submit(
                request,
                submission_version=submission_version,
                idempotency_key=idempotency_key,
            )

        execution = await self._ledger.execute(
            execution,
            invoke,
            business_write=success_write(),
            outcome_projection=projection_for,
            outcome_events=events_for,
        )
        return await self._submission_result(request, execution, submission_version)

    async def _authorize_selection_event(
        self,
        event: SelectionDecisionRecorded | SelectionCompleted,
        ctx: Context,
    ) -> str:
        if event.tenant_id != ctx.tenant_id:
            raise PermissionError("selection event tenant does not match the trusted context")
        return await _authorize("selection:event:apply", event.payload.campaign_id, ctx)

    async def _record_selection_event(
        self,
        event: SelectionDecisionRecorded | SelectionCompleted,
        consumed: ConsumedIntegrationInbox,
        *,
        policy_version: str,
        ctx: Context,
    ) -> SelectionEventResult:
        record = consumed.record
        if (
            event.tenant_id != ctx.tenant_id
            or record.tenant_id != ctx.tenant_id
            or record.adapter_id != event.adapter_id
            or record.source_event_id != event.source_event_id
            or record.event_type != event.event_type
            or record.processing_status != "consumed"
            or record.payload_hash != integration_payload_hash(event)
            or consumed.wait.resource_type != "campaign"
            or consumed.wait.resource_id != event.payload.campaign_id
            or consumed.wait.expected_version != event.version
            or consumed.wait.event_type != event.event_type
        ):
            raise PermissionError("selection event source binding is not trusted")
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name=(
                "record_selection_decision"
                if isinstance(event, SelectionDecisionRecorded)
                else "complete_selection"
            ),
            tool_schema_version=1,
            schema=type(event.payload),
            args=event.payload.model_dump(),
            stable_business_id=f"{event.adapter_id}:{event.source_event_id}",
            checkpoint_id=consumed.wait.checkpoint_id,
            request_idempotency_key=f"integration:{event.adapter_id}:{event.source_event_id}",
        )
        if execution.status == "succeeded":
            binding = await self._required_binding(event.payload.campaign_id, ctx)
            return SelectionEventResult(
                event_type=event.event_type,
                selection_version=event.payload.selection_version,
                execution_id=execution.execution_id,
                idempotency_key=execution.idempotency_key,
                approval_binding=binding,
                invalidation_status="not_required",
            )
        if execution.status != "reserved":
            raise RuntimeError("prior selection event execution is not retryable")
        now = self._now()
        invalidation_fact: ApprovalBindingInvalidationFact | None = None
        selection_version = event.payload.selection_version
        if isinstance(event, SelectionDecisionRecorded):
            decision_payload = event.payload
            decision = SelectionDecision(
                tenant_id=ctx.tenant_id,
                selection_decision_id=_stable_id(
                    "selection_decision",
                    ctx.tenant_id,
                    decision_payload.campaign_id,
                    decision_payload.selection_version,
                    decision_payload.enrollment_item_id,
                ),
                campaign_id=decision_payload.campaign_id,
                submission_version=decision_payload.submission_version,
                selection_version=decision_payload.selection_version,
                enrollment_item_id=decision_payload.enrollment_item_id,
                decision=decision_payload.decision,
                reason_code=decision_payload.reason_code,
                version=1,
                created_at=now,
                updated_at=now,
            )

            async def write(session: AsyncSession) -> None:
                await self._repository.record_selection_decision(session, decision=decision)

            binding = await self._required_binding(decision_payload.campaign_id, ctx)
        else:
            completion_payload = event.payload
            binding = await self._required_binding(completion_payload.campaign_id, ctx)
            selection_hash = await self._repository.selection_completion_hash(
                tenant_id=ctx.tenant_id,
                campaign_id=completion_payload.campaign_id,
                submission_version=completion_payload.submission_version,
                selection_version=completion_payload.selection_version,
            )
            updated_binding = binding.model_copy(
                update={
                    "selection_version": completion_payload.selection_version,
                    "selection_hash": selection_hash,
                }
            )
            invalidation_fact = ApprovalBindingInvalidationFact(
                event_id=_stable_id(
                    "selection_binding",
                    ctx.tenant_id,
                    completion_payload.campaign_id,
                    completion_payload.selection_version,
                ),
                tenant_id=ctx.tenant_id,
                binding=updated_binding,
                reason="selection_version_completed",
                occurred_at=now,
            )

            async def write(session: AsyncSession) -> None:
                await self._repository.complete_selection(
                    session,
                    tenant_id=ctx.tenant_id,
                    campaign_id=completion_payload.campaign_id,
                    submission_version=completion_payload.submission_version,
                    selection_version=completion_payload.selection_version,
                    expected_binding=binding,
                    updated_binding=updated_binding,
                    updated_at=now,
                )

            binding = updated_binding
        events = _execution_events(
            now=now,
            execution=execution,
            aggregate_type="selection",
            aggregate_id=selection_version,
            event_type=event.event_type,
            count=1,
            outcome="succeeded",
            policy_version=policy_version,
            ctx=ctx,
            binding_fact=invalidation_fact,
        )
        execution = await self._ledger.record_local_success(
            execution,
            receipt_id=_stable_id("receipt", execution.idempotency_key),
            business_write=write,
            domain_events=events.domain_events,
            audit_events=events.audit_events,
            outbox_records=events.outbox_records,
        )
        invalidation_status: Literal["not_required", "applied", "reconciliation"] = "not_required"
        if invalidation_fact is not None:
            try:
                result = await self._approval_invalidator.consume(invalidation_fact)
                invalidation_status = (
                    "applied" if getattr(result, "status", None) == "applied" else "reconciliation"
                )
            except Exception:
                invalidation_status = "reconciliation"
        return SelectionEventResult(
            event_type=event.event_type,
            selection_version=selection_version,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            approval_binding=binding,
            invalidation_status=invalidation_status,
        )

    async def request_consumer_publish_approval(
        self,
        request: PublishConsumerPlacementArgs,
        *,
        expires_at: datetime,
        ctx: Context,
    ) -> Approval:
        canonical = _canonical_publish_request(request)
        args_hash = canonical_args_hash(
            tool_name=PUBLISH_CONSUMER_TOOL_NAME,
            tool_schema_version=1,
            schema=PublishConsumerPlacementArgs,
            args=canonical.model_dump(),
        )
        selection = await self._repository.load_selection(
            tenant_id=ctx.tenant_id,
            campaign_id=request.campaign_id,
            selection_version=request.selection_version,
        )
        _require_publishable_selection(selection)
        return await self._approvals.create(
            approval_action="consumer_publish_approval",
            tool_name=PUBLISH_CONSUMER_TOOL_NAME,
            canonical_args_hash=args_hash,
            checkpoint_id=ctx.run_id,
            expires_at=expires_at,
            ctx=ctx,
            business_binding=selection.binding,
        )

    async def publish_consumer_placement(
        self,
        request: PublishConsumerPlacementArgs,
        ctx: Context,
    ) -> PublishConsumerPlacementResult:
        canonical = _canonical_publish_request(request)
        spec_hash = _hash(request.placement_spec)
        placement_id = _stable_id(
            "consumer_placement",
            ctx.tenant_id,
            request.campaign_id,
            request.selection_version,
            spec_hash,
        )
        selection = await self._repository.load_selection(
            tenant_id=ctx.tenant_id,
            campaign_id=request.campaign_id,
            selection_version=request.selection_version,
        )
        selected_ids = _require_publishable_selection(selection)
        args_hash = canonical_args_hash(
            tool_name=PUBLISH_CONSUMER_TOOL_NAME,
            tool_schema_version=1,
            schema=PublishConsumerPlacementArgs,
            args=canonical.model_dump(),
        )
        await self._approvals.authorize_resume(
            request=ApprovalResumeRequest(
                approval_id=request.approval_id,
                approval_action="consumer_publish_approval",
                tool_name=PUBLISH_CONSUMER_TOOL_NAME,
                canonical_args_hash=args_hash,
                checkpoint_id=ctx.run_id,
                approval_required=True,
                business_binding=selection.binding,
            ),
            tool_policy=PUBLISH_CONSUMER_POLICY,
            ctx=ctx,
        )
        policy_version = await _authorize("consumer:publish", request.campaign_id, ctx)
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name=PUBLISH_CONSUMER_TOOL_NAME,
            tool_schema_version=1,
            schema=PublishConsumerPlacementArgs,
            args=canonical.model_dump(),
            stable_business_id=(f"{request.campaign_id}:{request.selection_version}:{spec_hash}"),
            checkpoint_id=ctx.run_id,
            request_idempotency_key=request.idempotency_key,
        )
        if args_hash != execution.canonical_args_hash:
            raise RuntimeError("consumer placement canonical binding changed during execution")
        now = self._now()

        def placement_for(outcome: ExecutionOutcome) -> ConsumerPlacement:
            status = {"succeeded": "published", "failed": "failed", "unknown": "unknown"}[outcome]
            return ConsumerPlacement(
                tenant_id=ctx.tenant_id,
                consumer_placement_id=placement_id,
                campaign_id=request.campaign_id,
                selection_version=request.selection_version,
                placement_spec_hash=spec_hash,
                status=status,  # type: ignore[arg-type]
                request_id=execution.execution_id,
                receipt_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )

        def success_write() -> BusinessMutation:
            placement = placement_for("succeeded")

            async def write(session: AsyncSession) -> None:
                await self._repository.persist_placement_outcome(
                    session,
                    placement=placement,
                    expected_binding=selection.binding,
                    selected_item_ids=selected_ids,
                    outcome="succeeded",
                )

            return write

        def projection_for(outcome: ProjectionOutcome) -> OutcomeProjectionMutation:
            return self._repository.placement_outcome_projection(
                execution_id=execution.execution_id,
                placement=placement_for(outcome),
            )

        def events_for(outcome: ExecutionOutcome) -> ExecutionEventBundle:
            return _execution_events(
                now=now,
                execution=execution,
                aggregate_type="consumer_placement",
                aggregate_id=placement_id,
                event_type=f"consumer.placement_{outcome}",
                count=len(selected_ids),
                outcome=outcome,
                policy_version=policy_version,
                ctx=ctx,
            )

        if execution.status == "executing":
            events = events_for("unknown")
            execution = await self._ledger.recover_stale_executing(
                execution,
                outcome_projection=projection_for("unknown"),
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
            if execution.status == "executing":
                return self._waiting_placement_result(
                    request,
                    execution,
                    placement_id,
                    spec_hash,
                    selected_ids,
                    now,
                )
        if execution.status in {"succeeded", "failed", "unknown"}:
            return await self._placement_result(request, execution, placement_id)
        if execution.status != "reserved":
            raise RuntimeError("consumer placement execution state is not replayable")

        async def invoke(idempotency_key: str) -> Receipt:
            return await self._placement_adapter.publish(
                request,
                selected_item_ids=selected_ids,
                idempotency_key=idempotency_key,
            )

        execution = await self._ledger.execute(
            execution,
            invoke,
            business_write=success_write(),
            outcome_projection=projection_for,
            outcome_events=events_for,
        )
        return await self._placement_result(request, execution, placement_id)

    async def request_notification_approval(
        self,
        request: SendMerchantNotificationArgs,
        *,
        expires_at: datetime,
        ctx: Context,
    ) -> Approval:
        reasons = self._notification_adapter.capabilities.approval_reasons(request.template_id)
        if not reasons:
            raise ValueError("merchant notification does not require conditional approval")
        canonical = _canonical_notification_request(request)
        args_hash = canonical_args_hash(
            tool_name=SEND_NOTIFICATION_TOOL_NAME,
            tool_schema_version=1,
            schema=SendMerchantNotificationArgs,
            args=canonical.model_dump(),
        )
        binding = await self._required_binding(request.campaign_id, ctx)
        return await self._approvals.create(
            approval_action="merchant_notification_approval",
            tool_name=SEND_NOTIFICATION_TOOL_NAME,
            canonical_args_hash=args_hash,
            checkpoint_id=ctx.run_id,
            expires_at=expires_at,
            ctx=ctx,
            business_binding=binding,
        )

    async def send_merchant_notification(
        self,
        request: SendMerchantNotificationArgs,
        ctx: Context,
    ) -> SendMerchantNotificationResult:
        canonical = _canonical_notification_request(request)
        notification_id = _stable_id(
            "merchant_notification",
            ctx.tenant_id,
            request.merchant_id,
            request.campaign_id,
            request.result_version,
            request.template_id,
            request.channel,
        )
        message = await self._repository.notification_message(
            tenant_id=ctx.tenant_id,
            merchant_id=request.merchant_id,
            campaign_id=request.campaign_id,
            result_version=request.result_version,
            template_id=request.template_id,
            channel=request.channel,
        )
        binding = await self._required_binding(request.campaign_id, ctx)
        reasons = self._notification_adapter.capabilities.approval_reasons(request.template_id)
        args_hash = canonical_args_hash(
            tool_name=SEND_NOTIFICATION_TOOL_NAME,
            tool_schema_version=1,
            schema=SendMerchantNotificationArgs,
            args=canonical.model_dump(),
        )
        await self._approvals.authorize_resume(
            request=ApprovalResumeRequest(
                approval_id=request.approval_id,
                approval_action="merchant_notification_approval",
                tool_name=SEND_NOTIFICATION_TOOL_NAME,
                canonical_args_hash=args_hash,
                checkpoint_id=ctx.run_id,
                approval_required=bool(reasons),
                business_binding=binding,
            ),
            tool_policy=SEND_NOTIFICATION_POLICY,
            ctx=ctx,
        )
        policy_version = await _authorize("notification:send", request.campaign_id, ctx)
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name=SEND_NOTIFICATION_TOOL_NAME,
            tool_schema_version=1,
            schema=SendMerchantNotificationArgs,
            args=canonical.model_dump(),
            stable_business_id=notification_id,
            checkpoint_id=ctx.run_id,
            request_idempotency_key=request.idempotency_key,
        )
        if args_hash != execution.canonical_args_hash:
            raise RuntimeError("notification canonical binding changed during execution")
        now = self._now()
        attempts = [0]

        def notification_for(outcome: ExecutionOutcome) -> MerchantNotification:
            return MerchantNotification(
                tenant_id=ctx.tenant_id,
                merchant_notification_id=notification_id,
                merchant_id=request.merchant_id,
                campaign_id=request.campaign_id,
                result_version=request.result_version,
                template_id=request.template_id,
                channel=request.channel,
                status="sent" if outcome == "succeeded" else "dead_letter",
                attempt_count=max(attempts[0], 1),
                receipt_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )

        def success_write() -> BusinessMutation:
            notification = notification_for("succeeded")

            async def write(session: AsyncSession) -> None:
                await self._repository.persist_notification_outcome(
                    session,
                    notification=notification,
                )

            return write

        def projection_for(outcome: ProjectionOutcome) -> OutcomeProjectionMutation:
            return self._repository.notification_outcome_projection(
                execution_id=execution.execution_id,
                notification=notification_for(outcome),
                outcome=outcome,
            )

        def events_for(outcome: ExecutionOutcome) -> ExecutionEventBundle:
            return _execution_events(
                now=now,
                execution=execution,
                aggregate_type="merchant_notification",
                aggregate_id=notification_id,
                event_type=f"merchant.notification_{outcome}",
                count=max(attempts[0], 1),
                outcome=outcome,
                policy_version=policy_version,
                ctx=ctx,
            )

        if execution.status == "executing":
            events = events_for("unknown")
            execution = await self._ledger.recover_stale_executing(
                execution,
                outcome_projection=projection_for("unknown"),
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
            if execution.status == "executing":
                return self._waiting_notification_result(
                    request,
                    execution,
                    notification_id,
                    now,
                )
        if execution.status in {"succeeded", "failed", "unknown"}:
            return await self._notification_result(request, execution, notification_id)
        if execution.status != "reserved":
            raise RuntimeError("merchant notification execution state is not replayable")

        async def invoke(idempotency_key: str) -> Receipt:
            last: Receipt | None = None
            for attempt in range(1, self._notification_adapter.capabilities.max_attempts + 1):
                attempts[0] = attempt
                last = await self._notification_adapter.send(
                    message,
                    idempotency_key=idempotency_key,
                    attempt=attempt,
                )
                if last.status != "rejected":
                    return last
            if last is None:
                raise RuntimeError("notification adapter made no delivery attempt")
            return last

        execution = await self._ledger.execute(
            execution,
            invoke,
            business_write=success_write(),
            outcome_projection=projection_for,
            outcome_events=events_for,
        )
        return await self._notification_result(request, execution, notification_id)

    async def _assortment_precheck(
        self,
        request: SubmitAssortmentArgs,
        ctx: Context,
    ) -> tuple[
        SubmitAssortmentArgs,
        str,
        ApprovalBusinessBinding,
        AssortmentCandidateSet,
        Campaign,
        ProductEligibilityCriteria,
    ]:
        campaign = await self._required_campaign(request.campaign_id, ctx)
        if campaign.status not in {"recruiting", "selecting"}:
            raise ValueError("campaign is not accepting assortment submissions")
        rule_ref = await self._rule_refs.get(campaign.rule_snapshot_ref_id, ctx)
        if rule_ref is None:
            raise LookupError("campaign rule snapshot is unavailable")
        snapshot = await self._rule_snapshots.get(rule_ref.snapshot_id, ctx)
        if (
            snapshot.tenant_id != ctx.tenant_id
            or snapshot.snapshot_hash != rule_ref.snapshot_hash
            or snapshot.recompute_hash() != snapshot.snapshot_hash
            or request.assortment_policy_ref != snapshot.enrollment_policy.assortment_policy_ref
            or request.assortment_policy_version
            != snapshot.enrollment_policy.assortment_policy_version
        ):
            raise PermissionError("assortment policy does not match the frozen rule snapshot")
        canonical = request.model_copy(
            update={
                "enrollment_item_ids": tuple(sorted(request.enrollment_item_ids)),
                "idempotency_key": "[request-key]",
                "approval_id": None,
            }
        )
        args_hash = canonical_args_hash(
            tool_name=SUBMIT_ASSORTMENT_TOOL_NAME,
            tool_schema_version=1,
            schema=SubmitAssortmentArgs,
            args=canonical.model_dump(),
        )
        binding = await self._required_binding(request.campaign_id, ctx)
        product_criteria = ProductEligibilityCriteria.from_snapshot(snapshot)
        candidate_set = await self._repository.load_submission_candidates(
            tenant_id=ctx.tenant_id,
            campaign_id=request.campaign_id,
            rule_snapshot_ref_id=campaign.rule_snapshot_ref_id,
            product_criteria=product_criteria,
            assortment_policy_ref=request.assortment_policy_ref,
            assortment_policy_version=request.assortment_policy_version,
            approval_binding=binding,
        )
        if not set(request.enrollment_item_ids).issubset(candidate_set.enrollment_item_ids):
            raise PermissionError("assortment items are outside the server candidate set")
        return canonical, args_hash, binding, candidate_set, campaign, product_criteria

    async def _required_campaign(self, campaign_id: str, ctx: Context) -> Campaign:
        campaign = await self._campaigns.get(campaign_id, ctx)
        if campaign is None:
            raise LookupError("campaign is unavailable")
        return campaign

    async def _required_binding(self, campaign_id: str, ctx: Context) -> ApprovalBusinessBinding:
        binding = await self._repository.get_approval_binding(
            tenant_id=ctx.tenant_id,
            campaign_id=campaign_id,
        )
        if binding is None:
            raise LookupError("campaign approval business binding is unavailable")
        return binding

    @staticmethod
    def _waiting_submission_result(
        request: SubmitAssortmentArgs,
        execution: ToolExecution,
        submission_version: str,
        item_ids: tuple[str, ...],
        now: datetime,
    ) -> SubmitAssortmentResult:
        return SubmitAssortmentResult(
            submission=AssortmentSubmission(
                tenant_id=execution.tenant_id,
                assortment_submission_id=_stable_id(
                    "assortment_submission",
                    execution.tenant_id,
                    request.campaign_id,
                    submission_version,
                ),
                campaign_id=request.campaign_id,
                submission_version=submission_version,
                assortment_policy_ref=request.assortment_policy_ref,
                assortment_policy_version=request.assortment_policy_version,
                status="pending",
                version=1,
                created_at=execution.created_at,
                updated_at=now,
            ),
            enrollment_item_ids=item_ids,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status="waiting",
        )

    @staticmethod
    def _waiting_placement_result(
        request: PublishConsumerPlacementArgs,
        execution: ToolExecution,
        placement_id: str,
        spec_hash: str,
        selected_item_ids: tuple[str, ...],
        now: datetime,
    ) -> PublishConsumerPlacementResult:
        return PublishConsumerPlacementResult(
            placement=ConsumerPlacement(
                tenant_id=execution.tenant_id,
                consumer_placement_id=placement_id,
                campaign_id=request.campaign_id,
                selection_version=request.selection_version,
                placement_spec_hash=spec_hash,
                status="pending",
                request_id=execution.execution_id,
                version=1,
                created_at=execution.created_at,
                updated_at=now,
            ),
            selected_item_ids=selected_item_ids,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status="waiting",
        )

    @staticmethod
    def _waiting_notification_result(
        request: SendMerchantNotificationArgs,
        execution: ToolExecution,
        notification_id: str,
        now: datetime,
    ) -> SendMerchantNotificationResult:
        return SendMerchantNotificationResult(
            notification=MerchantNotification(
                tenant_id=execution.tenant_id,
                merchant_notification_id=notification_id,
                merchant_id=request.merchant_id,
                campaign_id=request.campaign_id,
                result_version=request.result_version,
                template_id=request.template_id,
                channel=request.channel,
                status="pending",
                attempt_count=execution.attempt_count,
                version=1,
                created_at=execution.created_at,
                updated_at=now,
            ),
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status="waiting",
        )

    async def _submission_result(
        self,
        request: SubmitAssortmentArgs,
        execution: ToolExecution,
        submission_version: str,
    ) -> SubmitAssortmentResult:
        submission, item_ids = await self._repository.load_submission(
            tenant_id=execution.tenant_id,
            campaign_id=request.campaign_id,
            submission_version=submission_version,
        )
        return SubmitAssortmentResult(
            submission=submission,
            enrollment_item_ids=item_ids,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status=_replay_status(execution),
        )

    async def _placement_result(
        self,
        request: PublishConsumerPlacementArgs,
        execution: ToolExecution,
        placement_id: str,
    ) -> PublishConsumerPlacementResult:
        placement = await self._repository.load_placement(
            tenant_id=execution.tenant_id,
            placement_id=placement_id,
        )
        selection = await self._repository.load_selection(
            tenant_id=execution.tenant_id,
            campaign_id=request.campaign_id,
            selection_version=request.selection_version,
        )
        return PublishConsumerPlacementResult(
            placement=placement,
            selected_item_ids=selection.selected_item_ids,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status=_replay_status(execution),
        )

    async def _notification_result(
        self,
        request: SendMerchantNotificationArgs,
        execution: ToolExecution,
        notification_id: str,
    ) -> SendMerchantNotificationResult:
        notification = await self._repository.load_notification(
            tenant_id=execution.tenant_id,
            notification_id=notification_id,
        )
        return SendMerchantNotificationResult(
            notification=notification,
            execution_id=execution.execution_id,
            idempotency_key=execution.idempotency_key,
            request_idempotency_key=request.idempotency_key,
            replay_status=_replay_status(execution),
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("assortment service clock must return a timezone-aware timestamp")
        return now


def _canonical_publish_request(
    request: PublishConsumerPlacementArgs,
) -> PublishConsumerPlacementArgs:
    return request.model_copy(update={"idempotency_key": "[request-key]", "approval_id": None})


def _replay_status(
    execution: ToolExecution,
) -> Literal["completed", "waiting", "reconciliation"]:
    if execution.status == "executing":
        return "waiting"
    if execution.status == "unknown":
        return "reconciliation"
    return "completed"


def _canonical_notification_request(
    request: SendMerchantNotificationArgs,
) -> SendMerchantNotificationArgs:
    return request.model_copy(update={"idempotency_key": "[request-key]", "approval_id": None})


def _require_publishable_selection(selection: AssortmentSelection) -> tuple[str, ...]:
    if selection.campaign.status != "pending_consumer_publish":
        raise ValueError("campaign is not ready for consumer publication")
    if selection.submission.status != "completed":
        raise ValueError("assortment submission is not completed")
    if not selection.decisions:
        raise ValueError("selection result has no decisions")
    decision_versions = {decision.selection_version for decision in selection.decisions}
    if decision_versions != {selection.binding.selection_version}:
        raise PermissionError("selection binding is stale")
    decision_item_ids = tuple(decision.enrollment_item_id for decision in selection.decisions)
    if len(decision_item_ids) != len(set(decision_item_ids)):
        raise ValueError("selection result contains duplicate item decisions")
    if set(decision_item_ids) != set(selection.enrollment_item_ids):
        raise ValueError("selection result does not cover the submitted item set")
    sealed_hash = selection_result_hash(
        campaign_id=selection.campaign.campaign_id,
        submission_version=selection.submission.submission_version,
        selection_version=selection.binding.selection_version,
        decisions=selection.decisions,
    )
    if (
        selection.submission.selection_version != selection.binding.selection_version
        or selection.submission.selection_hash != sealed_hash
        or selection.binding.selection_hash != sealed_hash
    ):
        raise PermissionError("selection result seal is stale")
    selected = selection.selected_item_ids
    if not selected:
        raise ValueError("consumer placement requires at least one selected item")
    active_link_ids = {
        link.enrollment_item_id for link in selection.links if link.status == "active"
    }
    if any(item_id not in active_link_ids for item_id in selected):
        raise ValueError("selected item does not have an active coupon link")
    return selected


async def _authorize(action: str, campaign_id: str, ctx: Context) -> str:
    decision = await ctx.policy.authorize(
        AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=action,
            resource=ResourceRef(
                resource_type="campaign",
                resource_id=campaign_id,
                tenant_id=ctx.tenant_id,
            ),
            context=AuthorizationContext(correlation_id=ctx.correlation_id),
        ),
        ctx,
    )
    if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
        raise PermissionError("assortment workflow write is not authorized")
    return decision.policy_version


def _execution_events(
    *,
    now: datetime,
    execution: ToolExecution,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    count: int,
    outcome: ExecutionOutcome,
    policy_version: str,
    ctx: Context,
    binding_fact: ApprovalBindingInvalidationFact | None = None,
) -> ExecutionEventBundle:
    event_id = f"domain_event_{uuid.uuid4().hex}"
    payload: dict[str, JsonValue] = {
        "args_hash": execution.canonical_args_hash,
        "count": count,
        "execution_id": execution.execution_id,
        "outcome": outcome,
    }
    outbox = [
        OutboxRecord(
            event_id=event_id,
            tenant_id=ctx.tenant_id,
            topic=event_type,
            payload_json=json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            occurred_at=now,
            available_at=now,
        )
    ]
    if binding_fact is not None:
        outbox.append(
            OutboxRecord(
                event_id=binding_fact.event_id,
                tenant_id=ctx.tenant_id,
                topic="selection.version_completed",
                payload_json=json.dumps(
                    {
                        **binding_fact.binding.model_dump(mode="json"),
                        "reason": binding_fact.reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                occurred_at=now,
                available_at=now,
            )
        )
    return ExecutionEventBundle(
        domain_events=(
            DomainEvent(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                event_version=1,
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
                    resource_type=aggregate_type,
                    resource_id=aggregate_id,
                    tenant_id=ctx.tenant_id,
                ),
                decision="allow",
                policy_version=policy_version,
                args_hash=execution.canonical_args_hash,
                result="success" if outcome == "succeeded" else "failure",
                correlation_id=ctx.correlation_id,
                payload={"count": count, "execution_id": execution.execution_id},
            ),
        ),
        outbox_records=tuple(outbox),
    )
