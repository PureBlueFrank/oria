"""Strict parameter and model-visible result contracts for V0.1 tools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from oria.core.types import CitationBlock, ValueModel
from oria.domain.launch import (
    CampaignDraft,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.domain.ledger import LaunchPlan
from oria.domain.models import (
    BasicRule,
    BenefitRule,
    ConfirmationRule,
    EligibilityReason,
    EnrollmentRule,
    MerchantMaterialRule,
)
from oria.domain.product_eligibility import ProductEligibilityReason, ProductSnapshot


class SearchCampaignRulesParams(ValueModel):
    intent: Literal["merchant_recruitment"]
    effective_at: datetime

    @field_validator("effective_at")
    @classmethod
    def require_aware_effective_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_at must include a timezone")
        return value


class PublicRecruitmentScope(ValueModel):
    categories: tuple[str, ...]
    cities: tuple[str, ...]
    enrollment_systems: tuple[str, ...]


class PublicCampaignRules(ValueModel):
    basic: BasicRule
    recruitment_scope: PublicRecruitmentScope
    enrollment_policy: EnrollmentRule
    benefit_policy: BenefitRule
    confirmation_policy: ConfirmationRule
    merchant_material: MerchantMaterialRule


class SearchCampaignRulesResult(ValueModel):
    schema_version: Literal[1] = 1
    rule_snapshot_id: str | None = Field(default=None, pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    snapshot_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    effective_at: datetime
    rules: PublicCampaignRules | None = None
    field_evidence: dict[str, CitationBlock] = Field(default_factory=dict)
    unresolved_items: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_snapshot_or_unresolved_items(self) -> Self:
        complete = (
            self.rule_snapshot_id is not None
            and self.snapshot_hash is not None
            and self.rules is not None
            and bool(self.field_evidence)
            and not self.unresolved_items
        )
        unresolved = (
            self.rule_snapshot_id is None
            and self.snapshot_hash is None
            and self.rules is None
            and not self.field_evidence
            and bool(self.unresolved_items)
        )
        if not (complete or unresolved):
            raise ValueError("rule search result must be complete or explicitly unresolved")
        return self


class QueryMerchantsParams(ValueModel):
    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    limit: int = Field(ge=1, le=100)


class MerchantCandidate(ValueModel):
    merchant_id: str
    version: int = Field(ge=1)
    display_name: str
    categories: tuple[str, ...]
    cities: tuple[str, ...]
    enrollment_systems: tuple[str, ...]
    eligibility: Literal["eligible"] = "eligible"


class QueryMerchantsResult(ValueModel):
    schema_version: Literal[1] = 1
    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    candidates: tuple[MerchantCandidate, ...]
    exclusion_reason_counts: dict[EligibilityReason, int]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.returned_count != len(self.candidates) or self.returned_count > self.eligible_count:
            raise ValueError("returned_count must match the bounded candidate projection")
        if self.evaluated_count != self.eligible_count + self.excluded_count:
            raise ValueError("merchant counts must partition evaluated_count")
        if sum(self.exclusion_reason_counts.values()) < self.excluded_count:
            raise ValueError("exclusion reason counts do not explain every exclusion")
        return self


class QueryEligibleProductsParams(ValueModel):
    campaign_id: str = Field(min_length=1)
    merchant_ids: tuple[str, ...] = Field(min_length=1)
    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    product_circle_policy_ref: str = Field(min_length=1)
    product_circle_policy_version: str = Field(min_length=1)
    cursor: str | None = Field(default=None, min_length=1)
    limit: int = Field(ge=1, le=100)

    @field_validator("merchant_ids")
    @classmethod
    def require_unique_merchants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("merchant_ids must be non-empty and unique")
        return value


class QueryEligibleProductsResult(ValueModel):
    schema_version: Literal[1] = 1
    campaign_id: str
    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    product_circle_policy_ref: str
    product_circle_policy_version: str
    catalog_snapshot_id: str
    evaluated_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    products: tuple[ProductSnapshot, ...]
    exclusion_reason_counts: dict[ProductEligibilityReason, int]
    next_cursor: str | None = None

    @model_validator(mode="after")
    def validate_product_counts(self) -> Self:
        if self.eligible_count != len(self.products):
            raise ValueError("eligible_count must match the returned product snapshots")
        if self.evaluated_count != self.eligible_count + self.excluded_count:
            raise ValueError("product counts must partition evaluated_count")
        if sum(self.exclusion_reason_counts.values()) < self.excluded_count:
            raise ValueError("exclusion reasons must explain every excluded product")
        return self


class PersistCampaignDraftParams(ValueModel):
    campaign_id: str = Field(min_length=1)
    coupon_batch_id: str = Field(min_length=1)
    recruitment_publication_id: str = Field(min_length=1)
    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    material_version: str = Field(min_length=1)
    compensation_policy_version: str = Field(min_length=1)


class PersistCampaignDraftResult(ValueModel):
    schema_version: Literal[1] = 1
    campaign_id: str
    campaign_status: Literal["draft"]
    campaign_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rule_snapshot_id: str
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coupon_batch_id: str
    coupon_batch_status: Literal["draft"]
    coupon_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coupon_batch_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recruitment_publication_id: str
    merchant_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    material_version: str
    compensation_policy_version: str

    @classmethod
    def from_draft(cls, draft: CampaignDraft) -> PersistCampaignDraftResult:
        if draft.campaign.status != "draft" or draft.coupon_batch.status != "draft":
            raise ValueError("campaign draft result requires unlaunched draft resources")
        return cls(
            campaign_id=draft.campaign.campaign_id,
            campaign_status="draft",
            campaign_draft_hash=draft.campaign_draft_hash,
            rule_snapshot_id=draft.rule_snapshot_ref.snapshot_id,
            rule_snapshot_hash=draft.rule_snapshot_ref.snapshot_hash,
            coupon_batch_id=draft.coupon_batch.coupon_batch_id,
            coupon_batch_status="draft",
            coupon_spec_hash=draft.coupon_batch.coupon_spec_hash,
            coupon_batch_draft_hash=draft.coupon_batch_draft_hash,
            recruitment_publication_id=(draft.recruitment_publication.recruitment_publication_id),
            merchant_scope_hash=draft.recruitment_publication.merchant_scope_hash,
            material_version=draft.recruitment_publication.material_version,
            compensation_policy_version=draft.compensation_policy_version,
        )


class LaunchApprovalParams(ValueModel):
    draft: CampaignDraft
    materialize_args: MaterializeCouponBatchArgs
    publish_args: PublishRecruitmentArgs
    checkpoint_id: str = Field(min_length=1)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class LaunchApprovalResult(ValueModel):
    schema_version: Literal[1] = 1
    approval_id: str
    approval_status: Literal["pending"]
    checkpoint_id: str
    plan: LaunchPlan


class MaterializeCouponBatchParams(ValueModel):
    args: MaterializeCouponBatchArgs
    plan: LaunchPlan
    approval_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)


class PublishRecruitmentParams(ValueModel):
    args: PublishRecruitmentArgs
    plan: LaunchPlan
    approval_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)


class LaunchChildExecutionResult(ValueModel):
    schema_version: Literal[1] = 1
    execution_id: str
    tool_name: Literal["materialize_coupon_batch", "publish_recruitment"]
    status: Literal["reserved", "executing", "succeeded", "failed", "unknown"]
    idempotency_key: str
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
