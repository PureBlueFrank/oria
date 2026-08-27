"""Pure validation contracts for campaign rule snapshots."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oria.domain.models import (
    BasicRule,
    BenefitRule,
    BenefitStep,
    BenefitTierRule,
    EnrollmentRule,
)
from oria.rag.models import DocumentIngestRequest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "window",
    [
        "not-an-interval",
        "2026-08-01T00:00:00+08:00/2026-07-01T00:00:00+08:00",
        "2026-07-01T00:00:00/2026-08-01T00:00:00+08:00",
    ],
)
def test_basic_rule_rejects_invalid_or_reversed_aware_windows(window: str) -> None:
    with pytest.raises(ValidationError):
        BasicRule(
            template_ref="template-v1",
            product_scope=("eligible_products",),
            campaign_type="merchant_recruitment",
            campaign_window=window,
            enrollment_window="2026-07-02T00:00:00+08:00/2026-07-31T00:00:00+08:00",
        )


def test_basic_rule_rejects_enrollment_window_outside_campaign_window() -> None:
    with pytest.raises(ValidationError):
        BasicRule(
            template_ref="template-v1",
            product_scope=("eligible_products",),
            campaign_type="merchant_recruitment",
            campaign_window="2026-07-01T00:00:00+08:00/2026-08-01T00:00:00+08:00",
            enrollment_window="2026-06-30T00:00:00+08:00/2026-07-31T00:00:00+08:00",
        )


def test_benefit_rule_rejects_duplicate_or_empty_tiers() -> None:
    with pytest.raises(ValidationError):
        BenefitRule(
            tiers=("base", "base"),
            tier_rules=(
                BenefitTierRule(name="base", funding_type="fixed_amount", fixed_amount="5"),
            ),
            currency="CNY",
            rounding="half_up",
            budget_cap="1000",
        )
    with pytest.raises(ValidationError):
        BenefitRule(
            tiers=(),
            tier_rules=(),
            currency="CNY",
            rounding="half_up",
            budget_cap="1000",
        )


@pytest.mark.parametrize("discount_rate", ["0", "1", "1.2", "-0.1"])
def test_benefit_rule_rejects_invalid_discount_rates(discount_rate: str) -> None:
    with pytest.raises(ValidationError):
        BenefitTierRule(
            name="base",
            funding_type="discount_rate",
            discount_rate=discount_rate,
        )


def test_benefit_rule_rejects_unsorted_or_ambiguous_steps() -> None:
    with pytest.raises(ValidationError):
        BenefitTierRule(
            name="boosted",
            funding_type="stepped",
            steps=(
                BenefitStep(threshold="200", funding_amount="20"),
                BenefitStep(threshold="100", funding_amount="10"),
            ),
        )


def test_enrollment_rule_rejects_missing_product_and_assortment_policies() -> None:
    with pytest.raises(ValidationError):
        EnrollmentRule(
            mode="hybrid",
            linked_campaign_rules=("campaign-rule-v1",),
            accepted_sources=("merchant", "auto"),
        )
    with pytest.raises(ValidationError):
        BenefitTierRule(
            name="boosted",
            funding_type="stepped",
            fixed_amount="10",
            steps=(
                BenefitStep(threshold="100", funding_amount="10"),
                BenefitStep(threshold="200", funding_amount="20"),
            ),
        )


@pytest.mark.parametrize(
    ("document_id", "version"),
    [("../escape", "1"), ("valid", "../1"), ("bad space", "1")],
)
def test_ingest_identity_rejects_path_or_namespace_confusion(
    document_id: str, version: str
) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestRequest(
            document_id=document_id,
            version=version,
            source_uri="package://fixture",
            owner_ref="fixture",
            data_classification="internal",
            content="content",
        )
