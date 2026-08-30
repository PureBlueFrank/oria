"""Validated campaign drafts and launch workflow domain services."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import Field, model_validator
from pydantic_core import ValidationError

from oria.core.approvals import (
    Approval,
    ApprovalResumeRequest,
    ApprovalService,
    canonical_args_hash,
)
from oria.core.execution_ledger import (
    ExecutionEventBundle,
    ExecutionLedger,
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
    Campaign,
    CampaignRuleSnapshotRef,
    CouponBatch,
    LaunchSagaState,
    RecruitmentPublication,
)
from oria.domain.ledger import (
    DomainEvent,
    LaunchChildStep,
    LaunchPlan,
    OutboxRecord,
    Receipt,
    ToolExecution,
)
from oria.domain.models import BasicRule, BenefitRule, MerchantMaterialRule
from oria.domain.repositories import CampaignDraftRepository, CampaignLaunchRepository
from oria.rag.models import CampaignRuleSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oria.core.context import Context


LAUNCH_PLAN_TOOL_NAME = "LaunchPlan"
MATERIALIZE_TOOL_NAME = "materialize_coupon_batch"
PUBLISH_TOOL_NAME = "publish_recruitment"
COMPENSATE_TOOL_NAME = "compensate_coupon_batch"

LAUNCH_PLAN_POLICY = ToolPolicy(
    risk_level="high",
    side_effect=True,
    timeout_seconds=30,
    retry_policy=RetryPolicy(max_attempts=1),
    idempotency_scope="launch_plan",
    required_action="campaign:launch:request",
    resource_type="campaign",
    approval_mode="required",
    approval_action="launch_approval",
)
MATERIALIZE_TOOL_POLICY = LAUNCH_PLAN_POLICY.model_copy(
    update={"idempotency_scope": "campaign_id:coupon_spec_hash"}
)
PUBLISH_TOOL_POLICY = LAUNCH_PLAN_POLICY.model_copy(
    update={"idempotency_scope": "campaign_id:merchant_scope_hash:material_version"}
)


def _binding_hash(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class CampaignDraftSpec(ValueModel):
    campaign_id: str = Field(min_length=1)
    coupon_batch_id: str = Field(min_length=1)
    recruitment_publication_id: str = Field(min_length=1)
    material_version: str = Field(min_length=1)
    compensation_policy_version: str = Field(min_length=1)


class CampaignDraft(ValueModel):
    """Immutable local draft facts used to build the later LaunchPlan."""

    campaign: Campaign
    rule_snapshot_ref: CampaignRuleSnapshotRef
    coupon_batch: CouponBatch
    recruitment_publication: RecruitmentPublication
    compensation_policy_version: str = Field(min_length=1)
    campaign_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coupon_batch_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def compute_coupon_batch_draft_hash(cls, coupon_batch: CouponBatch) -> str:
        return _binding_hash(
            {
                "campaign_id": coupon_batch.campaign_id,
                "coupon_batch_id": coupon_batch.coupon_batch_id,
                "coupon_spec_hash": coupon_batch.coupon_spec_hash,
                "status": coupon_batch.status,
                "tenant_id": coupon_batch.tenant_id,
                "version": coupon_batch.version,
            }
        )

    @classmethod
    def compute_campaign_draft_hash(
        cls,
        *,
        campaign: Campaign,
        rule_snapshot_ref: CampaignRuleSnapshotRef,
        coupon_batch_draft_hash: str,
        recruitment_publication: RecruitmentPublication,
        compensation_policy_version: str,
    ) -> str:
        return _binding_hash(
            {
                "campaign_id": campaign.campaign_id,
                "campaign_status": campaign.status,
                "campaign_version": campaign.version,
                "compensation_policy_version": compensation_policy_version,
                "coupon_batch_draft_hash": coupon_batch_draft_hash,
                "enrollment_mode": campaign.enrollment_mode,
                "material_version": recruitment_publication.material_version,
                "merchant_scope_hash": recruitment_publication.merchant_scope_hash,
                "recruitment_publication_id": (recruitment_publication.recruitment_publication_id),
                "rule_snapshot_id": rule_snapshot_ref.snapshot_id,
                "rule_snapshot_hash": rule_snapshot_ref.snapshot_hash,
                "rule_snapshot_ref_id": rule_snapshot_ref.campaign_rule_snapshot_ref_id,
                "tenant_id": campaign.tenant_id,
            }
        )

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        entities = (
            self.rule_snapshot_ref,
            self.coupon_batch,
            self.recruitment_publication,
        )
        self.campaign.validate_tenant_links(*entities)
        if (
            self.campaign.rule_snapshot_ref_id
            != self.rule_snapshot_ref.campaign_rule_snapshot_ref_id
        ):
            raise ValueError("campaign draft rule reference is inconsistent")
        if any(
            campaign_id != self.campaign.campaign_id
            for campaign_id in (
                self.coupon_batch.campaign_id,
                self.recruitment_publication.campaign_id,
            )
        ):
            raise ValueError("campaign draft resources are inconsistent")
        if (
            self.campaign.status != "draft"
            or self.coupon_batch.status != "draft"
            or self.recruitment_publication.status != "pending"
        ):
            raise ValueError("campaign draft resources must remain unlaunched")
        expected_coupon = self.compute_coupon_batch_draft_hash(self.coupon_batch)
        if self.coupon_batch_draft_hash != expected_coupon:
            raise ValueError("coupon_batch_draft_hash does not match the draft")
        expected_campaign = self.compute_campaign_draft_hash(
            campaign=self.campaign,
            rule_snapshot_ref=self.rule_snapshot_ref,
            coupon_batch_draft_hash=self.coupon_batch_draft_hash,
            recruitment_publication=self.recruitment_publication,
            compensation_policy_version=self.compensation_policy_version,
        )
        if self.campaign_draft_hash != expected_campaign:
            raise ValueError("campaign_draft_hash does not match the draft")
        return self


class MaterializeCouponBatchArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    coupon_batch_id: str = Field(min_length=1)
    coupon_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PublishRecruitmentArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    recruitment_publication_id: str = Field(min_length=1)
    merchant_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    material_version: str = Field(min_length=1)


class CompensateCouponBatchArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    coupon_batch_id: str = Field(min_length=1)
    coupon_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    materialization_execution_id: str = Field(min_length=1)
    compensation_policy_version: str = Field(min_length=1)


class LaunchApprovalBinding(ValueModel):
    plan: LaunchPlan
    approval: Approval


class LaunchExecutionRequest(ValueModel):
    plan: LaunchPlan
    approval_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    materialize_args: MaterializeCouponBatchArgs
    publish_args: PublishRecruitmentArgs


class LaunchSagaResult(ValueModel):
    saga: LaunchSagaState
    materialize_execution: ToolExecution | None = None
    publish_execution: ToolExecution | None = None
    compensation_execution: ToolExecution | None = None


class CouponBatchAdapter(Protocol):
    adapter_id: str
    idempotent_compensation_contract_verified: bool

    async def materialize(
        self,
        args: MaterializeCouponBatchArgs,
        *,
        idempotency_key: str,
    ) -> Receipt: ...

    async def compensate(
        self,
        args: CompensateCouponBatchArgs,
        *,
        idempotency_key: str,
    ) -> Receipt: ...


class RecruitmentAdapter(Protocol):
    adapter_id: str

    async def publish(
        self,
        args: PublishRecruitmentArgs,
        *,
        idempotency_key: str,
    ) -> Receipt: ...


class CompensationPolicyRegistry(ValueModel):
    verified_idempotent_versions: frozenset[str] = frozenset()

    def allows_automatic_compensation(self, version: str) -> bool:
        return version in self.verified_idempotent_versions


class CampaignLaunchService(Protocol):
    async def persist_campaign_draft(
        self,
        spec: CampaignDraftSpec,
        snapshot: CampaignRuleSnapshot,
        ctx: Context,
    ) -> CampaignDraft: ...

    async def request_launch_approval(
        self,
        *,
        draft: CampaignDraft,
        materialize_args: MaterializeCouponBatchArgs,
        publish_args: PublishRecruitmentArgs,
        checkpoint_id: str,
        expires_at: datetime,
        ctx: Context,
    ) -> LaunchApprovalBinding: ...

    async def execute_launch(
        self,
        request: LaunchExecutionRequest,
        ctx: Context,
    ) -> LaunchSagaResult: ...

    async def materialize_coupon_batch(
        self,
        *,
        args: MaterializeCouponBatchArgs,
        plan: LaunchPlan,
        approval_id: str,
        checkpoint_id: str,
        ctx: Context,
    ) -> ToolExecution: ...

    async def publish_recruitment(
        self,
        *,
        args: PublishRecruitmentArgs,
        plan: LaunchPlan,
        approval_id: str,
        checkpoint_id: str,
        ctx: Context,
    ) -> ToolExecution: ...


class DefaultCampaignLaunchService:
    """Own validated local draft writes; external launch steps are added separately."""

    def __init__(
        self,
        drafts: CampaignDraftRepository,
        *,
        launches: CampaignLaunchRepository | None = None,
        approvals: ApprovalService | None = None,
        ledger: ExecutionLedger | None = None,
        coupon_adapter: CouponBatchAdapter | None = None,
        recruitment_adapter: RecruitmentAdapter | None = None,
        compensation_policies: CompensationPolicyRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rule_ref_id_factory: Callable[[], str] = lambda: f"rule_ref_{uuid.uuid4().hex}",
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}_{uuid.uuid4().hex}",
    ) -> None:
        self._drafts = drafts
        self._launches = launches
        self._approvals = approvals
        self._ledger = ledger
        self._coupon_adapter = coupon_adapter
        self._recruitment_adapter = recruitment_adapter
        self._compensation_policies = compensation_policies or CompensationPolicyRegistry()
        self._clock = clock
        self._rule_ref_id_factory = rule_ref_id_factory
        self._id_factory = id_factory

    async def persist_campaign_draft(
        self,
        spec: CampaignDraftSpec,
        snapshot: CampaignRuleSnapshot,
        ctx: Context,
    ) -> CampaignDraft:
        _validate_rule_snapshot(snapshot, ctx.tenant_id)
        await _authorize_campaign_write("campaign:draft:write", spec.campaign_id, ctx)
        now = self._now()
        rule_ref = CampaignRuleSnapshotRef(
            campaign_rule_snapshot_ref_id=self._rule_ref_id_factory(),
            tenant_id=ctx.tenant_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            version=1,
            created_at=now,
            updated_at=now,
        )
        campaign = Campaign(
            campaign_id=spec.campaign_id,
            tenant_id=ctx.tenant_id,
            rule_snapshot_ref_id=rule_ref.campaign_rule_snapshot_ref_id,
            enrollment_mode=snapshot.enrollment_policy.mode,
            version=1,
            created_at=now,
            updated_at=now,
        )
        coupon_spec_hash = _binding_hash(snapshot.benefit_policy.model_dump(mode="json"))
        coupon_batch = CouponBatch(
            coupon_batch_id=spec.coupon_batch_id,
            tenant_id=ctx.tenant_id,
            campaign_id=spec.campaign_id,
            coupon_spec_hash=coupon_spec_hash,
            version=1,
            created_at=now,
            updated_at=now,
        )
        scope_payload = snapshot.internal_payload()["recruitment_scope"]
        recruitment_publication = RecruitmentPublication(
            recruitment_publication_id=spec.recruitment_publication_id,
            tenant_id=ctx.tenant_id,
            campaign_id=spec.campaign_id,
            merchant_scope_hash=_binding_hash(scope_payload),
            material_version=spec.material_version,
            status="pending",
            version=1,
            created_at=now,
            updated_at=now,
        )
        coupon_batch_draft_hash = CampaignDraft.compute_coupon_batch_draft_hash(coupon_batch)
        draft = CampaignDraft(
            campaign=campaign,
            rule_snapshot_ref=rule_ref,
            coupon_batch=coupon_batch,
            recruitment_publication=recruitment_publication,
            compensation_policy_version=spec.compensation_policy_version,
            coupon_batch_draft_hash=coupon_batch_draft_hash,
            campaign_draft_hash=CampaignDraft.compute_campaign_draft_hash(
                campaign=campaign,
                rule_snapshot_ref=rule_ref,
                coupon_batch_draft_hash=coupon_batch_draft_hash,
                recruitment_publication=recruitment_publication,
                compensation_policy_version=spec.compensation_policy_version,
            ),
        )
        await self._drafts.create_bundle(
            rule_snapshot_ref=rule_ref,
            campaign=campaign,
            coupon_batch=coupon_batch,
            recruitment_publication=recruitment_publication,
            ctx=ctx,
        )
        return draft

    async def request_launch_approval(
        self,
        *,
        draft: CampaignDraft,
        materialize_args: MaterializeCouponBatchArgs,
        publish_args: PublishRecruitmentArgs,
        checkpoint_id: str,
        expires_at: datetime,
        ctx: Context,
    ) -> LaunchApprovalBinding:
        launches, approvals, _, _, _ = self._require_launch_components()
        current = await launches.load_draft_entities(
            campaign_id=draft.campaign.campaign_id,
            rule_snapshot_ref_id=draft.rule_snapshot_ref.campaign_rule_snapshot_ref_id,
            coupon_batch_id=draft.coupon_batch.coupon_batch_id,
            recruitment_publication_id=(draft.recruitment_publication.recruitment_publication_id),
            ctx=ctx,
        )
        if current != (
            draft.campaign,
            draft.rule_snapshot_ref,
            draft.coupon_batch,
            draft.recruitment_publication,
        ):
            raise PermissionError("campaign draft binding no longer matches persisted facts")
        plan = self.build_launch_plan(
            draft=draft,
            materialize_args=materialize_args,
            publish_args=publish_args,
        )
        approval = await approvals.create(
            approval_action="launch_approval",
            tool_name=LAUNCH_PLAN_TOOL_NAME,
            canonical_args_hash=plan.plan_hash,
            checkpoint_id=checkpoint_id,
            expires_at=expires_at,
            ctx=ctx,
        )
        return LaunchApprovalBinding(plan=plan, approval=approval)

    @staticmethod
    def build_launch_plan(
        *,
        draft: CampaignDraft,
        materialize_args: MaterializeCouponBatchArgs,
        publish_args: PublishRecruitmentArgs,
    ) -> LaunchPlan:
        if (
            materialize_args.campaign_id != draft.campaign.campaign_id
            or materialize_args.coupon_batch_id != draft.coupon_batch.coupon_batch_id
            or materialize_args.coupon_spec_hash != draft.coupon_batch.coupon_spec_hash
        ):
            raise ValueError("coupon materialization args do not match the campaign draft")
        publication = draft.recruitment_publication
        if (
            publish_args.campaign_id != draft.campaign.campaign_id
            or publish_args.recruitment_publication_id != publication.recruitment_publication_id
            or publish_args.merchant_scope_hash != publication.merchant_scope_hash
            or publish_args.material_version != publication.material_version
        ):
            raise ValueError("recruitment publication args do not match the campaign draft")
        materialize_hash = canonical_args_hash(
            tool_name=MATERIALIZE_TOOL_NAME,
            tool_schema_version=1,
            schema=MaterializeCouponBatchArgs,
            args=materialize_args.model_dump(),
        )
        publish_hash = canonical_args_hash(
            tool_name=PUBLISH_TOOL_NAME,
            tool_schema_version=1,
            schema=PublishRecruitmentArgs,
            args=publish_args.model_dump(),
        )
        child_steps = [
            LaunchChildStep(
                tool_name=MATERIALIZE_TOOL_NAME,
                canonical_args_hash=materialize_hash,
                idempotency_scope=(
                    f"{materialize_args.campaign_id}:{materialize_args.coupon_spec_hash}"
                ),
            ),
            LaunchChildStep(
                tool_name=PUBLISH_TOOL_NAME,
                canonical_args_hash=publish_hash,
                idempotency_scope=(
                    f"{publish_args.campaign_id}:{publish_args.merchant_scope_hash}:"
                    f"{publish_args.material_version}"
                ),
            ),
        ]
        plan_hash = LaunchPlan.compute_plan_hash(
            campaign_draft_id=draft.campaign.campaign_id,
            campaign_draft_hash=draft.campaign_draft_hash,
            rule_snapshot_id=draft.rule_snapshot_ref.snapshot_id,
            rule_snapshot_hash=draft.rule_snapshot_ref.snapshot_hash,
            coupon_batch_draft_id=draft.coupon_batch.coupon_batch_id,
            coupon_batch_draft_hash=draft.coupon_batch_draft_hash,
            merchant_scope_hash=publication.merchant_scope_hash,
            material_version=publication.material_version,
            child_steps=child_steps,
            compensation_policy_version=draft.compensation_policy_version,
        )
        return LaunchPlan(
            campaign_draft_id=draft.campaign.campaign_id,
            campaign_draft_hash=draft.campaign_draft_hash,
            rule_snapshot_id=draft.rule_snapshot_ref.snapshot_id,
            rule_snapshot_hash=draft.rule_snapshot_ref.snapshot_hash,
            coupon_batch_draft_id=draft.coupon_batch.coupon_batch_id,
            coupon_batch_draft_hash=draft.coupon_batch_draft_hash,
            merchant_scope_hash=publication.merchant_scope_hash,
            material_version=publication.material_version,
            child_steps=child_steps,
            compensation_policy_version=draft.compensation_policy_version,
            plan_hash=plan_hash,
        )

    async def decide_launch_approval(
        self,
        *,
        approval_id: str,
        decision: Literal["approve", "reject"],
        reason: str | None,
        ctx: Context,
    ) -> Approval:
        _, approvals, _, _, _ = self._require_launch_components()
        return await approvals.decide(
            tenant_id=ctx.tenant_id,
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            ctx=ctx,
        )

    async def execute_launch(
        self,
        request: LaunchExecutionRequest,
        ctx: Context,
    ) -> LaunchSagaResult:
        launches, _, _, _, _ = self._require_launch_components()
        approval = await self._authorize_plan(
            plan=request.plan,
            approval_id=request.approval_id,
            checkpoint_id=request.checkpoint_id,
            ctx=ctx,
        )
        try:
            LaunchPlan.model_validate(request.plan.model_dump())
            self._validate_plan_args(request)
        except (PermissionError, ValidationError, ValueError) as exc:
            await self._invalidate(approval, ctx)
            raise PermissionError("launch plan no longer matches its approved binding") from exc
        saga = await launches.get_saga(request.plan.campaign_draft_id, ctx)
        if saga is None:
            now = self._now()
            saga = await launches.create_saga(
                LaunchSagaState(
                    launch_saga_id=self._id_factory("launch_saga"),
                    tenant_id=ctx.tenant_id,
                    campaign_id=request.plan.campaign_draft_id,
                    status="planned",
                    checkpoint=request.checkpoint_id,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
                ctx,
            )
        if saga.checkpoint != request.checkpoint_id:
            approval = await self._authorize_plan(
                plan=request.plan,
                approval_id=request.approval_id,
                checkpoint_id=request.checkpoint_id,
                ctx=ctx,
            )
            await self._invalidate(approval, ctx)
            raise PermissionError("launch saga checkpoint does not match the approval")
        if saga.status in {"completed", "reconciliation_required", "failed"}:
            return LaunchSagaResult(saga=saga)

        materialize_execution: ToolExecution | None = None
        publish_execution: ToolExecution | None = None
        compensation_execution: ToolExecution | None = None
        if saga.status == "compensation_pending":
            materialize_execution = await self.materialize_coupon_batch(
                args=request.materialize_args,
                plan=request.plan,
                approval_id=request.approval_id,
                checkpoint_id=request.checkpoint_id,
                ctx=ctx,
            )
            compensation_execution = await self._compensate_coupon_batch(
                request=request,
                materialization_execution_id=materialize_execution.execution_id,
                ctx=ctx,
            )
            return LaunchSagaResult(
                saga=saga,
                materialize_execution=materialize_execution,
                compensation_execution=compensation_execution,
            )
        if saga.status == "planned":
            try:
                materialize_execution = await self.materialize_coupon_batch(
                    args=request.materialize_args,
                    plan=request.plan,
                    approval_id=request.approval_id,
                    checkpoint_id=request.checkpoint_id,
                    ctx=ctx,
                )
            except Exception:
                saga = await launches.transition_saga(saga, "failed", self._now(), ctx)
                raise
            if materialize_execution.status == "unknown":
                saga = await launches.transition_saga(
                    saga, "reconciliation_required", self._now(), ctx
                )
                return LaunchSagaResult(saga=saga, materialize_execution=materialize_execution)
            if materialize_execution.status != "succeeded":
                saga = await launches.transition_saga(saga, "failed", self._now(), ctx)
                return LaunchSagaResult(saga=saga, materialize_execution=materialize_execution)
            saga = await launches.transition_saga(saga, "coupon_materialized", self._now(), ctx)

        if saga.status == "coupon_materialized":
            if materialize_execution is None:
                materialize_execution = await self.materialize_coupon_batch(
                    args=request.materialize_args,
                    plan=request.plan,
                    approval_id=request.approval_id,
                    checkpoint_id=request.checkpoint_id,
                    ctx=ctx,
                )
            try:
                publish_execution = await self.publish_recruitment(
                    args=request.publish_args,
                    plan=request.plan,
                    approval_id=request.approval_id,
                    checkpoint_id=request.checkpoint_id,
                    ctx=ctx,
                )
            except Exception:
                return await self._handle_publish_failure(
                    saga=saga,
                    request=request,
                    materialize_execution=materialize_execution,
                    publish_execution=None,
                    ctx=ctx,
                )
            if publish_execution.status == "unknown":
                saga = await launches.transition_saga(
                    saga, "reconciliation_required", self._now(), ctx
                )
                return LaunchSagaResult(
                    saga=saga,
                    materialize_execution=materialize_execution,
                    publish_execution=publish_execution,
                )
            if publish_execution.status != "succeeded":
                return await self._handle_publish_failure(
                    saga=saga,
                    request=request,
                    materialize_execution=materialize_execution,
                    publish_execution=publish_execution,
                    ctx=ctx,
                )
            saga = await launches.transition_saga(saga, "recruitment_published", self._now(), ctx)

        if saga.status == "recruitment_published":
            saga = await launches.transition_saga(saga, "completed", self._now(), ctx)
        return LaunchSagaResult(
            saga=saga,
            materialize_execution=materialize_execution,
            publish_execution=publish_execution,
            compensation_execution=compensation_execution,
        )

    async def materialize_coupon_batch(
        self,
        *,
        args: MaterializeCouponBatchArgs,
        plan: LaunchPlan,
        approval_id: str,
        checkpoint_id: str,
        ctx: Context,
    ) -> ToolExecution:
        launches, _, ledger, coupon_adapter, _ = self._require_launch_components()
        execution = await ledger.reserve_for_args(
            execution_id=self._id_factory("tool_execution"),
            tenant_id=ctx.tenant_id,
            tool_name=MATERIALIZE_TOOL_NAME,
            tool_schema_version=1,
            schema=MaterializeCouponBatchArgs,
            args=args.model_dump(),
            stable_business_id=f"{args.campaign_id}:{args.coupon_spec_hash}",
            checkpoint_id=checkpoint_id,
        )
        approval = await self._authorize_plan(
            plan=plan,
            approval_id=approval_id,
            checkpoint_id=checkpoint_id,
            ctx=ctx,
        )
        await self._validate_child_hash(
            plan=plan,
            tool_name=MATERIALIZE_TOOL_NAME,
            observed_hash=execution.canonical_args_hash,
            approval=approval,
            ctx=ctx,
        )
        saga = await launches.get_saga(args.campaign_id, ctx)
        if saga is None or saga.checkpoint != checkpoint_id:
            await self._invalidate(approval, ctx)
            raise PermissionError("coupon materialization requires the approved saga checkpoint")
        if saga.status not in {"planned", "coupon_materialized", "compensation_pending"}:
            raise PermissionError("coupon materialization is not allowed at this saga checkpoint")
        if execution.status != "reserved":
            return execution

        async def invoke(idempotency_key: str) -> Receipt:
            return await coupon_adapter.materialize(args, idempotency_key=idempotency_key)

        async def business_write(session: AsyncSession) -> None:
            await launches.mark_coupon_ready(
                session,
                tenant_id=ctx.tenant_id,
                coupon_batch_id=args.coupon_batch_id,
                updated_at=self._now(),
            )

        success_events = self._success_events(
            execution=execution,
            aggregate_type="coupon_batch",
            aggregate_id=args.coupon_batch_id,
            event_type="coupon_batch.materialized",
            approval=approval,
            ctx=ctx,
        )
        return await ledger.execute(
            execution,
            invoke,
            business_write=business_write,
            outcome_events=lambda outcome: (
                success_events if outcome == "succeeded" else ExecutionEventBundle()
            ),
        )

    async def publish_recruitment(
        self,
        *,
        args: PublishRecruitmentArgs,
        plan: LaunchPlan,
        approval_id: str,
        checkpoint_id: str,
        ctx: Context,
    ) -> ToolExecution:
        launches, _, ledger, _, recruitment_adapter = self._require_launch_components()
        execution = await ledger.reserve_for_args(
            execution_id=self._id_factory("tool_execution"),
            tenant_id=ctx.tenant_id,
            tool_name=PUBLISH_TOOL_NAME,
            tool_schema_version=1,
            schema=PublishRecruitmentArgs,
            args=args.model_dump(),
            stable_business_id=(
                f"{args.campaign_id}:{args.merchant_scope_hash}:{args.material_version}"
            ),
            checkpoint_id=checkpoint_id,
        )
        approval = await self._authorize_plan(
            plan=plan,
            approval_id=approval_id,
            checkpoint_id=checkpoint_id,
            ctx=ctx,
        )
        await self._validate_child_hash(
            plan=plan,
            tool_name=PUBLISH_TOOL_NAME,
            observed_hash=execution.canonical_args_hash,
            approval=approval,
            ctx=ctx,
        )
        saga = await launches.get_saga(args.campaign_id, ctx)
        if saga is None or saga.checkpoint != checkpoint_id:
            await self._invalidate(approval, ctx)
            raise PermissionError("recruitment publication requires the approved saga checkpoint")
        if saga.status not in {"coupon_materialized", "recruitment_published"}:
            raise PermissionError("recruitment publication is not allowed at this saga checkpoint")
        if execution.status != "reserved":
            return execution
        receipt: list[Receipt] = []

        async def invoke(idempotency_key: str) -> Receipt:
            result = await recruitment_adapter.publish(args, idempotency_key=idempotency_key)
            receipt.append(result)
            return result

        async def business_write(session: AsyncSession) -> None:
            if not receipt:
                raise RuntimeError("recruitment receipt is unavailable")
            observed = receipt[0]
            await launches.mark_recruitment_published(
                session,
                tenant_id=ctx.tenant_id,
                recruitment_publication_id=args.recruitment_publication_id,
                request_id=execution.execution_id,
                receipt_id=observed.receipt_id,
                updated_at=self._now(),
            )

        success_events = self._success_events(
            execution=execution,
            aggregate_type="recruitment_publication",
            aggregate_id=args.recruitment_publication_id,
            event_type="recruitment.published",
            approval=approval,
            ctx=ctx,
        )
        return await ledger.execute(
            execution,
            invoke,
            business_write=business_write,
            outcome_events=lambda outcome: (
                success_events if outcome == "succeeded" else ExecutionEventBundle()
            ),
        )

    async def _handle_publish_failure(
        self,
        *,
        saga: LaunchSagaState,
        request: LaunchExecutionRequest,
        materialize_execution: ToolExecution | None,
        publish_execution: ToolExecution | None,
        ctx: Context,
    ) -> LaunchSagaResult:
        launches, _, _, coupon_adapter, _ = self._require_launch_components()
        verified = (
            self._compensation_policies.allows_automatic_compensation(
                request.plan.compensation_policy_version
            )
            and coupon_adapter.idempotent_compensation_contract_verified
        )
        if not verified:
            reconciled = await launches.transition_saga(
                saga, "reconciliation_required", self._now(), ctx
            )
            return LaunchSagaResult(
                saga=reconciled,
                materialize_execution=materialize_execution,
                publish_execution=publish_execution,
            )
        pending = await launches.transition_saga(saga, "compensation_pending", self._now(), ctx)
        compensation = await self._compensate_coupon_batch(
            request=request,
            materialization_execution_id=(
                materialize_execution.execution_id
                if materialize_execution is not None
                else self._materialize_step(request.plan).canonical_args_hash
            ),
            ctx=ctx,
        )
        return LaunchSagaResult(
            saga=pending,
            materialize_execution=materialize_execution,
            publish_execution=publish_execution,
            compensation_execution=compensation,
        )

    async def _compensate_coupon_batch(
        self,
        *,
        request: LaunchExecutionRequest,
        materialization_execution_id: str,
        ctx: Context,
    ) -> ToolExecution:
        _, _, ledger, coupon_adapter, _ = self._require_launch_components()
        args = CompensateCouponBatchArgs(
            campaign_id=request.materialize_args.campaign_id,
            coupon_batch_id=request.materialize_args.coupon_batch_id,
            coupon_spec_hash=request.materialize_args.coupon_spec_hash,
            materialization_execution_id=materialization_execution_id,
            compensation_policy_version=request.plan.compensation_policy_version,
        )
        execution = await ledger.reserve_for_args(
            execution_id=self._id_factory("tool_execution"),
            tenant_id=ctx.tenant_id,
            tool_name=COMPENSATE_TOOL_NAME,
            tool_schema_version=1,
            schema=CompensateCouponBatchArgs,
            args=args.model_dump(),
            stable_business_id=(
                f"{args.campaign_id}:{args.coupon_spec_hash}:compensation:"
                f"{args.compensation_policy_version}"
            ),
            checkpoint_id=request.checkpoint_id,
        )
        if execution.status != "reserved":
            return execution

        async def invoke(idempotency_key: str) -> Receipt:
            return await coupon_adapter.compensate(args, idempotency_key=idempotency_key)

        return await ledger.execute(execution, invoke)

    async def _authorize_plan(
        self,
        *,
        plan: LaunchPlan,
        approval_id: str,
        checkpoint_id: str,
        ctx: Context,
    ) -> Approval:
        _, approvals, _, _, _ = self._require_launch_components()
        try:
            approval = await approvals.authorize_resume(
                request=ApprovalResumeRequest(
                    approval_id=approval_id,
                    approval_action="launch_approval",
                    tool_name=LAUNCH_PLAN_TOOL_NAME,
                    canonical_args_hash=plan.plan_hash,
                    checkpoint_id=checkpoint_id,
                ),
                tool_policy=LAUNCH_PLAN_POLICY,
                ctx=ctx,
            )
        except LookupError as exc:
            raise PermissionError("launch approval is unavailable") from exc
        if approval is None:
            raise PermissionError("launch approval is required")
        if approval.requester != ctx.actor.subject_id:
            await self._invalidate(approval, ctx)
            raise PermissionError("approval requester does not match the resume actor")
        return approval

    async def _validate_child_hash(
        self,
        *,
        plan: LaunchPlan,
        tool_name: str,
        observed_hash: str,
        approval: Approval,
        ctx: Context,
    ) -> None:
        step = next((item for item in plan.child_steps if item.tool_name == tool_name), None)
        if step is None or step.canonical_args_hash != observed_hash:
            await self._invalidate(approval, ctx)
            raise PermissionError("launch child arguments no longer match the approved plan")

    async def _invalidate(self, approval: Approval, ctx: Context) -> None:
        _, approvals, _, _, _ = self._require_launch_components()
        await approvals.invalidate_binding(approval, ctx=ctx)

    @staticmethod
    def _materialize_step(plan: LaunchPlan) -> LaunchChildStep:
        return next(step for step in plan.child_steps if step.tool_name == MATERIALIZE_TOOL_NAME)

    @staticmethod
    def _validate_plan_args(request: LaunchExecutionRequest) -> None:
        expected = {
            MATERIALIZE_TOOL_NAME: canonical_args_hash(
                tool_name=MATERIALIZE_TOOL_NAME,
                tool_schema_version=1,
                schema=MaterializeCouponBatchArgs,
                args=request.materialize_args.model_dump(),
            ),
            PUBLISH_TOOL_NAME: canonical_args_hash(
                tool_name=PUBLISH_TOOL_NAME,
                tool_schema_version=1,
                schema=PublishRecruitmentArgs,
                args=request.publish_args.model_dump(),
            ),
        }
        observed = {step.tool_name: step.canonical_args_hash for step in request.plan.child_steps}
        if observed != expected:
            raise PermissionError("launch step arguments do not match the approved plan")

    def _success_events(
        self,
        *,
        execution: ToolExecution,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        approval: Approval,
        ctx: Context,
    ) -> ExecutionEventBundle:
        now = self._now()
        event_id = self._id_factory("domain_event")
        payload: dict[str, JsonValue] = {
            "args_hash": execution.canonical_args_hash,
            "execution_id": execution.execution_id,
        }
        domain_event = DomainEvent(
            event_id=event_id,
            tenant_id=ctx.tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            event_version=1,
            payload=payload,
            occurred_at=now,
            correlation_id=ctx.correlation_id,
        )
        audit_event = EventEnvelope(
            event_id=self._id_factory("business_audit"),
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
            policy_version=approval.policy_version,
            args_hash=execution.canonical_args_hash,
            result="success",
            correlation_id=ctx.correlation_id,
            payload={"execution_id": execution.execution_id},
        )
        outbox = OutboxRecord(
            event_id=event_id,
            tenant_id=ctx.tenant_id,
            topic=event_type,
            payload_json=json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            occurred_at=now,
            available_at=now,
        )
        return ExecutionEventBundle((domain_event,), (audit_event,), (outbox,))

    def _require_launch_components(
        self,
    ) -> tuple[
        CampaignLaunchRepository,
        ApprovalService,
        ExecutionLedger,
        CouponBatchAdapter,
        RecruitmentAdapter,
    ]:
        components = (
            self._launches,
            self._approvals,
            self._ledger,
            self._coupon_adapter,
            self._recruitment_adapter,
        )
        if any(component is None for component in components):
            raise RuntimeError("campaign launch execution services are unavailable")
        return components  # type: ignore[return-value]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("campaign launch clock must return a timezone-aware timestamp")
        return now


def _validate_rule_snapshot(snapshot: CampaignRuleSnapshot, tenant_id: str) -> None:
    if snapshot.tenant_id != tenant_id:
        raise PermissionError("cross-tenant campaign rule snapshot is forbidden")
    benefit = snapshot.benefit_policy
    monetary_values: list[tuple[str, Decimal]] = [("budget_cap", benefit.budget_cap)]
    for index, tier in enumerate(benefit.tier_rules):
        if tier.fixed_amount is not None:
            monetary_values.append((f"tier[{index}].fixed_amount", tier.fixed_amount))
        if tier.discount_rate is not None:
            monetary_values.append((f"tier[{index}].discount_rate", tier.discount_rate))
        for step_index, step in enumerate(tier.steps):
            monetary_values.extend(
                (
                    (f"tier[{index}].steps[{step_index}].threshold", step.threshold),
                    (
                        f"tier[{index}].steps[{step_index}].funding_amount",
                        step.funding_amount,
                    ),
                )
            )
    for field_name, value in monetary_values:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{field_name} must be a finite positive Decimal")
    for index, tier in enumerate(benefit.tier_rules):
        if tier.discount_rate is not None and tier.discount_rate >= 1:
            raise ValueError(f"tier[{index}].discount_rate must be less than one")
    BenefitRule.model_validate(
        {
            "tiers": benefit.tiers,
            "tier_rules": [
                {
                    "name": tier.name,
                    "funding_type": tier.funding_type,
                    "fixed_amount": tier.fixed_amount,
                    "discount_rate": tier.discount_rate,
                    "steps": [
                        {
                            "threshold": step.threshold,
                            "funding_amount": step.funding_amount,
                        }
                        for step in tier.steps
                    ],
                }
                for tier in benefit.tier_rules
            ],
            "currency": benefit.currency,
            "rounding": benefit.rounding,
            "budget_cap": benefit.budget_cap,
        }
    )
    BasicRule.model_validate(
        {
            "template_ref": snapshot.basic.template_ref,
            "product_scope": snapshot.basic.product_scope,
            "campaign_type": snapshot.basic.campaign_type,
            "campaign_window": snapshot.basic.campaign_window,
            "enrollment_window": snapshot.basic.enrollment_window,
        }
    )
    material = snapshot.merchant_material
    MerchantMaterialRule.model_validate(
        {
            "title": material.title,
            "hero_image_ref": material.hero_image_ref,
            "introduction": material.introduction,
            "tags": material.tags,
        }
    )
    if not material.title.strip() or not material.introduction.strip():
        raise ValueError("merchant material title and introduction must be non-empty")
    if not material.hero_image_ref.startswith("object://"):
        raise ValueError("merchant material hero image must use object://")
    try:
        observed_hash = snapshot.recompute_hash()
    except (TypeError, ValueError) as exc:
        raise ValueError("campaign rule snapshot cannot be hashed") from exc
    if observed_hash != snapshot.snapshot_hash:
        raise ValueError("campaign rule snapshot hash does not match its payload")


async def _authorize_campaign_write(action: str, campaign_id: str, ctx: Context) -> None:
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
        raise PermissionError("campaign write is not authorized")
