"""V0.3-T05 deterministic product hard-rule tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.support.enrollment import NOW, product, snapshot

from oria.domain.business import EnrollmentItem
from oria.domain.product_eligibility import (
    ProductEligibilityCriteria,
    ProductEligibilityPolicy,
    ProductSnapshot,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"normalized_price": Decimal("19.999")}, "price_below_minimum"),
        ({"normalized_price": Decimal("500.001")}, "price_above_maximum"),
        ({"category": "零售"}, "category_mismatch"),
        ({"keyword_labels": ("冬季",)}, "keyword_mismatch"),
        (
            {"eligibility_facts": {"available": False, "status": "off"}},
            "product_unavailable",
        ),
    ],
)
def test_product_policy_excludes_each_hard_rule_with_a_stable_reason(
    updates: dict[str, object], reason: str
) -> None:
    criteria = ProductEligibilityCriteria.from_snapshot(snapshot())
    candidate = product().model_copy(update=updates)

    decision = ProductEligibilityPolicy().evaluate(candidate, criteria)

    assert decision.eligible is False
    assert reason in decision.reason_codes


def test_product_policy_accepts_normalized_decimal_boundaries_and_reports_eligible() -> None:
    criteria = ProductEligibilityCriteria.from_snapshot(snapshot())
    low = product().model_copy(update={"normalized_price": Decimal("20.0000")})
    high = product().model_copy(update={"normalized_price": Decimal("500.000")})

    assert ProductEligibilityPolicy().evaluate(low, criteria).reason_codes == ("eligible",)
    assert ProductEligibilityPolicy().evaluate(high, criteria).eligible is True
    assert low.normalized_price == Decimal("2E+1")


def test_complete_product_snapshot_requires_aware_capture_and_rejects_non_finite_price() -> None:
    values = product().model_dump()
    values["captured_at"] = datetime(2026, 7, 1)
    with pytest.raises(ValidationError, match="timezone"):
        ProductSnapshot.model_validate(values)
    values["captured_at"] = NOW
    values["normalized_price"] = Decimal("NaN")
    with pytest.raises(ValidationError, match="finite"):
        ProductSnapshot.model_validate(values)


def _item(mode: str, sources: frozenset[str]) -> EnrollmentItem:
    return EnrollmentItem(
        tenant_id="tenant-a",
        enrollment_item_id="item-1",
        enrollment_id="enrollment-1",
        campaign_id="campaign-1",
        merchant_id="merchant-1",
        product_ref="product-1",
        product_version="v1",
        product_snapshot_id="snapshot-1",
        mode=mode,  # type: ignore[arg-type]
        sources=sources,  # type: ignore[arg-type]
        status="pending_confirmation",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_enrollment_item_source_validation_and_hybrid_merge_are_mode_specific() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        _item("merchant", frozenset({"auto"}))
    with pytest.raises(ValidationError, match="incompatible"):
        _item("auto", frozenset({"merchant"}))

    hybrid = _item("hybrid", frozenset({"auto"}))
    merged = hybrid.merge_source("merchant", updated_at=NOW + timedelta(seconds=1))

    assert merged.sources == frozenset({"auto", "merchant"})
    assert merged.version == 2
    assert merged.merge_source("merchant", updated_at=NOW + timedelta(seconds=2)) == merged
    with pytest.raises(ValueError, match="incompatible"):
        _item("merchant", frozenset({"merchant"})).merge_source(
            "auto", updated_at=NOW + timedelta(seconds=1)
        )
