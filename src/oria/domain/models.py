"""Immutable V0.1 merchant and campaign-rule domain values."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from oria.core.types import ValueModel


class BasicRule(ValueModel):
    template_ref: str = Field(min_length=1)
    campaign_type: str = Field(min_length=1)
    campaign_window: str = Field(min_length=1)


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
    linked_campaign_rules: tuple[str, ...]
    accepted_sources: tuple[Literal["merchant", "auto"], ...] = Field(min_length=1)


class BenefitRule(ValueModel):
    tiers: tuple[Literal["base", "boosted"], ...] = Field(min_length=1)
    currency: str = Field(min_length=1)
    rounding: Literal["half_up"]


class ConfirmationRule(ValueModel):
    ordered_steps: tuple[Literal["merchant", "sales", "sales_manager"], ...] = Field(min_length=1)
    timeout_action: Literal["reject", "escalate", "explicit_auto_confirm"]


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
    merchants: tuple[Merchant, ...]
