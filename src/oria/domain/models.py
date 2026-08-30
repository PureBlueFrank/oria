"""Immutable V0.1 merchant and campaign-rule domain values."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from oria.core.types import ValueModel


class BasicRule(ValueModel):
    template_ref: str = Field(min_length=1)
    product_scope: tuple[str, ...] = Field(min_length=1)
    campaign_type: str = Field(min_length=1)
    campaign_window: str = Field(min_length=1)
    enrollment_window: str = Field(min_length=1)

    @field_validator("campaign_window", "enrollment_window")
    @classmethod
    def validate_window(cls, value: str) -> str:
        _parse_aware_interval(value)
        return value

    @model_validator(mode="after")
    def validate_enrollment_within_campaign(self) -> Self:
        campaign_start, campaign_end = _parse_aware_interval(self.campaign_window)
        enrollment_start, enrollment_end = _parse_aware_interval(self.enrollment_window)
        if enrollment_start < campaign_start or enrollment_end > campaign_end:
            raise ValueError("enrollment_window must be within campaign_window")
        return self


def _parse_aware_interval(value: str) -> tuple[datetime, datetime]:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError("rule window must be an ISO 8601 interval")
    try:
        start, end = (datetime.fromisoformat(part) for part in parts)
    except ValueError as exc:
        raise ValueError("rule window must be an ISO 8601 interval") from exc
    if any(item.tzinfo is None or item.utcoffset() is None for item in (start, end)):
        raise ValueError("rule window endpoints must include a timezone")
    if start >= end:
        raise ValueError("rule window must have a positive duration")
    return start, end


class RecruitmentScopeRule(ValueModel):
    categories: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = Field(min_length=1)
    enrollment_systems: tuple[str, ...] = Field(min_length=1)
    allowlist_merchant_ids: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)
    denylist_merchant_ids: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)
    sales_org_scope: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)

    def internal_allowlist(self) -> frozenset[str]:
        return frozenset(self.allowlist_merchant_ids)

    def internal_denylist(self) -> frozenset[str]:
        return frozenset(self.denylist_merchant_ids)

    def internal_sales_org_scope(self) -> frozenset[str]:
        return frozenset(self.sales_org_scope)


class EnrollmentRule(ValueModel):
    mode: Literal["merchant", "auto", "hybrid"]
    customer_selection_rule: str = Field(min_length=1)
    linked_campaign_rules: tuple[str, ...] = Field(min_length=1)
    accepted_sources: tuple[Literal["merchant", "auto"], ...] = Field(min_length=1)
    product_circle_policy_ref: str = Field(min_length=1)
    product_circle_policy_version: str = Field(min_length=1)
    product_price_min: Decimal = Field(ge=0)
    product_price_max: Decimal = Field(gt=0)
    product_categories: tuple[str, ...] = Field(min_length=1)
    product_keywords: tuple[str, ...] = Field(min_length=1)
    assortment_policy_ref: str = Field(min_length=1)
    assortment_policy_version: str = Field(min_length=1)
    assortment_execution_mode: Literal["async"]
    assortment_completion_condition: Literal["all_terminal", "minimum_selected"]

    @model_validator(mode="after")
    def validate_product_price_range(self) -> Self:
        if self.product_price_min >= self.product_price_max:
            raise ValueError("product price range must have a positive span")
        return self


class BenefitStep(ValueModel):
    threshold: Decimal = Field(gt=0)
    funding_amount: Decimal = Field(gt=0)


class BenefitTierRule(ValueModel):
    name: Literal["base", "boosted"]
    funding_type: Literal["fixed_amount", "stepped", "discount_rate"]
    fixed_amount: Decimal | None = Field(default=None, gt=0)
    discount_rate: Decimal | None = Field(default=None, gt=0, lt=1)
    steps: tuple[BenefitStep, ...] = ()

    @model_validator(mode="after")
    def validate_funding_shape(self) -> Self:
        if self.funding_type == "fixed_amount":
            valid = self.fixed_amount is not None and self.discount_rate is None and not self.steps
        elif self.funding_type == "discount_rate":
            valid = self.discount_rate is not None and self.fixed_amount is None and not self.steps
        else:
            thresholds = tuple(step.threshold for step in self.steps)
            funding = tuple(step.funding_amount for step in self.steps)
            valid = (
                self.fixed_amount is None
                and self.discount_rate is None
                and len(self.steps) >= 2
                and all(left < right for left, right in pairwise(thresholds))
                and all(left <= right for left, right in pairwise(funding))
            )
        if not valid:
            raise ValueError("benefit tier funding configuration is invalid")
        return self


class BenefitRule(ValueModel):
    tiers: tuple[Literal["base", "boosted"], ...] = Field(min_length=1)
    tier_rules: tuple[BenefitTierRule, ...] = Field(min_length=1)
    currency: str = Field(min_length=1)
    rounding: Literal["half_up"]
    budget_cap: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_tiers(self) -> Self:
        if len(set(self.tiers)) != len(self.tiers):
            raise ValueError("benefit tiers must be unique")
        if tuple(rule.name for rule in self.tier_rules) != self.tiers:
            raise ValueError("benefit tier names and definitions must match")
        return self


class ConfirmationRule(ValueModel):
    ordered_steps: tuple[Literal["merchant", "sales", "sales_manager"], ...]
    timeout_action: Literal["reject", "escalate", "explicit_auto_confirm"]

    @field_validator("ordered_steps")
    @classmethod
    def validate_ordered_steps(
        cls,
        value: tuple[Literal["merchant", "sales", "sales_manager"], ...],
    ) -> tuple[Literal["merchant", "sales", "sales_manager"], ...]:
        rank = {"merchant": 0, "sales": 1, "sales_manager": 2}
        if len(set(value)) != len(value) or tuple(sorted(value, key=rank.__getitem__)) != value:
            raise ValueError("confirmation steps must be unique and ordered by responsibility")
        return value


class MerchantMaterialRule(ValueModel):
    title: str = Field(min_length=1)
    hero_image_ref: str = Field(pattern=r"^object://")
    introduction: str = Field(min_length=1)
    tags: tuple[str, ...]


class EligibilityCriteria(ValueModel):
    rule_set_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    categories: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = Field(min_length=1)
    enrollment_systems: tuple[str, ...] = Field(min_length=1)
    allowlist_merchant_ids: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)
    denylist_merchant_ids: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)
    sales_org_scope: tuple[str, ...] = Field(min_length=1, repr=False, exclude=True)

    def internal_allowlist(self) -> frozenset[str]:
        return frozenset(self.allowlist_merchant_ids)

    def internal_denylist(self) -> frozenset[str]:
        return frozenset(self.denylist_merchant_ids)

    def internal_sales_org_scope(self) -> frozenset[str]:
        return frozenset(self.sales_org_scope)


class CampaignRuleSet(ValueModel):
    rule_set_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    basic: BasicRule
    recruitment_scope: RecruitmentScopeRule
    enrollment_policy: EnrollmentRule
    benefit_policy: BenefitRule
    confirmation_policy: ConfirmationRule
    merchant_material: MerchantMaterialRule

    def internal_eligibility_criteria(self) -> EligibilityCriteria:
        scope = self.recruitment_scope
        return EligibilityCriteria(
            rule_set_id=self.rule_set_id,
            rule_version=self.version,
            categories=scope.categories,
            cities=scope.cities,
            enrollment_systems=scope.enrollment_systems,
            allowlist_merchant_ids=tuple(sorted(scope.internal_allowlist())),
            denylist_merchant_ids=tuple(sorted(scope.internal_denylist())),
            sales_org_scope=tuple(sorted(scope.internal_sales_org_scope())),
        )


class MerchantSeed(ValueModel):
    merchant_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1)
    categories: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = Field(min_length=1)
    enrollment_systems: tuple[str, ...] = Field(min_length=1)
    sales_org_code: str = Field(min_length=1, repr=False, exclude=True)
    active: bool

    def internal_sales_org_code(self) -> str:
        return self.sales_org_code


class MerchantSeedSet(ValueModel):
    tenant_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    merchants: tuple[MerchantSeed, ...] = Field(min_length=1)


class MerchantRecord(ValueModel):
    tenant_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1)
    categories: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = Field(min_length=1)
    enrollment_systems: tuple[str, ...] = Field(min_length=1)
    sales_org_code: str = Field(min_length=1, repr=False, exclude=True)
    active: bool

    def internal_sales_org_code(self) -> str:
        return self.sales_org_code


class Merchant(ValueModel):
    tenant_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1)
    categories: tuple[str, ...]
    cities: tuple[str, ...]
    enrollment_systems: tuple[str, ...]


EligibilityReason = Literal[
    "eligible",
    "inactive",
    "category_mismatch",
    "city_mismatch",
    "not_allowlisted",
    "denylisted",
    "enrollment_system_mismatch",
    "sales_org_mismatch",
]


class EligibilityDecision(ValueModel):
    eligible: bool
    reason_codes: tuple[EligibilityReason, ...] = Field(min_length=1)


class EligibleMerchantSet(ValueModel):
    rule_set_id: str
    rule_version: str
    evaluated_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    merchants: tuple[Merchant, ...]
    exclusion_reason_counts: dict[EligibilityReason, int] = Field(default_factory=dict)
