"""Strict parameter and model-visible result contracts for V0.1 tools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from oria.core.types import CitationBlock, ValueModel
from oria.domain.models import (
    BasicRule,
    BenefitRule,
    ConfirmationRule,
    EligibilityReason,
    EnrollmentRule,
    MerchantMaterialRule,
)


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
