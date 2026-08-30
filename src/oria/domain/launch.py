"""Validated campaign drafts and launch workflow domain services."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, Self

from pydantic import Field, model_validator

from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    ResourceRef,
    ValueModel,
)
from oria.domain.business import (
    Campaign,
    CampaignRuleSnapshotRef,
    CouponBatch,
    RecruitmentPublication,
)
from oria.domain.models import BasicRule, BenefitRule, MerchantMaterialRule
from oria.domain.repositories import CampaignDraftRepository
from oria.rag.models import CampaignRuleSnapshot

if TYPE_CHECKING:
    from oria.core.context import Context


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


class CampaignLaunchService(Protocol):
    async def persist_campaign_draft(
        self,
        spec: CampaignDraftSpec,
        snapshot: CampaignRuleSnapshot,
        ctx: Context,
    ) -> CampaignDraft: ...


class DefaultCampaignLaunchService:
    """Own validated local draft writes; external launch steps are added separately."""

    def __init__(
        self,
        drafts: CampaignDraftRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rule_ref_id_factory: Callable[[], str] = lambda: f"rule_ref_{uuid.uuid4().hex}",
    ) -> None:
        self._drafts = drafts
        self._clock = clock
        self._rule_ref_id_factory = rule_ref_id_factory

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
