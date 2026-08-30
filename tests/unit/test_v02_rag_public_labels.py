"""Campaign-rule retrieval label regression tests."""

import pytest

from oria.rag.service import _public_section

pytestmark = pytest.mark.unit


def test_benefit_label_does_not_claim_an_unconfigured_discount_rate() -> None:
    section = {
        "tier_rules": [
            {"name": "base", "funding_type": "fixed_amount", "fixed_amount": "5.00"},
            {"name": "boosted", "funding_type": "stepped", "steps": []},
        ]
    }

    public = _public_section("benefit_policy", section)

    assert "折扣率" not in public


def test_benefit_label_includes_a_configured_discount_rate() -> None:
    section = {
        "tier_rules": [{"name": "base", "funding_type": "discount_rate", "discount_rate": "0.90"}]
    }

    public = _public_section("benefit_policy", section)

    assert "折扣率" in public
