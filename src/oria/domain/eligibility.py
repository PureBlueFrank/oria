"""Deterministic hard eligibility policy; no ranking or caller-supplied filters."""

from __future__ import annotations

from oria.domain.models import (
    EligibilityCriteria,
    EligibilityDecision,
    EligibilityReason,
    Merchant,
    MerchantRecord,
)


class EligibilityPolicy:
    """Apply every hard merchant condition with denylist precedence."""

    def evaluate(
        self,
        merchant: MerchantRecord,
        criteria: EligibilityCriteria,
    ) -> EligibilityDecision:
        reasons: list[EligibilityReason] = []
        if not merchant.active:
            reasons.append("inactive")
        if not set(merchant.categories).intersection(criteria.categories):
            reasons.append("category_mismatch")
        if not set(merchant.cities).intersection(criteria.cities):
            reasons.append("city_mismatch")
        if merchant.merchant_id not in criteria.internal_allowlist():
            reasons.append("not_allowlisted")
        if merchant.merchant_id in criteria.internal_denylist():
            reasons.append("denylisted")
        if not set(merchant.enrollment_systems).intersection(criteria.enrollment_systems):
            reasons.append("enrollment_system_mismatch")
        if merchant.internal_sales_org_code() not in criteria.internal_sales_org_scope():
            reasons.append("sales_org_mismatch")
        if reasons:
            return EligibilityDecision(eligible=False, reason_codes=tuple(reasons))
        return EligibilityDecision(eligible=True, reason_codes=("eligible",))

    def eligible_merchants(
        self,
        merchants: tuple[MerchantRecord, ...],
        criteria: EligibilityCriteria,
    ) -> tuple[Merchant, ...]:
        eligible: list[Merchant] = []
        for record in merchants:
            if not self.evaluate(record, criteria).eligible:
                continue
            eligible.append(
                Merchant(
                    tenant_id=record.tenant_id,
                    merchant_id=record.merchant_id,
                    version=record.version,
                    display_name=record.display_name,
                    categories=record.categories,
                    cities=record.cities,
                    enrollment_systems=record.enrollment_systems,
                )
            )
        return tuple(eligible)
