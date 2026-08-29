"""Direct-path unit assertions for T03 resources and hard eligibility."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from oria.domain.eligibility import EligibilityPolicy
from oria.domain.models import EligibilityCriteria, MerchantRecord
from oria.resources.loader import RULE_CATEGORIES, load_demo_data, verify_package_assets

pytestmark = pytest.mark.unit


def _criteria(**updates: object) -> EligibilityCriteria:
    values: dict[str, object] = {
        "rule_set_id": "rules-v1",
        "rule_version": "1.0.0",
        "categories": ("餐饮",),
        "cities": ("上海",),
        "enrollment_systems": ("demo-enroll",),
        "allowlist_merchant_ids": ("merchant-ok",),
        "denylist_merchant_ids": ("merchant-denied",),
        "sales_org_scope": ("east-a",),
    }
    values.update(updates)
    return EligibilityCriteria.model_validate(values)


def _merchant(**updates: object) -> MerchantRecord:
    values: dict[str, object] = {
        "tenant_id": "local-community",
        "merchant_id": "merchant-ok",
        "version": 1,
        "display_name": "虚构商家",
        "categories": ("餐饮",),
        "cities": ("上海",),
        "enrollment_systems": ("demo-enroll",),
        "sales_org_code": "east-a",
        "active": True,
    }
    values.update(updates)
    return MerchantRecord.model_validate(values)


def test_installed_resource_contract_declares_six_synthetic_rule_categories() -> None:
    manifest, heads = verify_package_assets()
    bundle = load_demo_data()

    assert manifest.rule_categories == RULE_CATEGORIES
    assert len(manifest.rule_categories) == 6
    assert manifest.dataset_id == "oria-synthetic-merchant-recruitment"
    assert manifest.version == bundle.rules.version == bundle.merchants.version == "1.0.0"
    assert heads == {"platform": "platform_0003", "business": "business_0001"}
    assert len(bundle.merchants.merchants) == 12


@pytest.mark.parametrize(
    ("merchant_updates", "expected_reason"),
    [
        ({"active": False}, "inactive"),
        ({"categories": ("零售",)}, "category_mismatch"),
        ({"cities": ("北京",)}, "city_mismatch"),
        ({"merchant_id": "merchant-other"}, "not_allowlisted"),
        ({"merchant_id": "merchant-denied"}, "denylisted"),
        ({"enrollment_systems": ("other",)}, "enrollment_system_mismatch"),
        ({"sales_org_code": "north"}, "sales_org_mismatch"),
    ],
)
def test_eligibility_policy_applies_every_hard_condition_with_and_semantics(
    merchant_updates: dict[str, object], expected_reason: str
) -> None:
    decision = EligibilityPolicy().evaluate(_merchant(**merchant_updates), _criteria())

    assert decision.eligible is False
    assert expected_reason in decision.reason_codes
    assert "eligible" not in decision.reason_codes


def test_eligibility_policy_is_deterministic_and_denylist_wins() -> None:
    policy = EligibilityPolicy()
    merchant = _merchant(merchant_id="merchant-denied")
    criteria = _criteria(allowlist_merchant_ids=("merchant-denied",))

    first = policy.evaluate(merchant, criteria)
    second = policy.evaluate(merchant, criteria)

    assert first == second
    assert first.eligible is False
    assert first.reason_codes == ("denylisted",)


@pytest.mark.parametrize(
    "field",
    [
        "categories",
        "cities",
        "enrollment_systems",
        "allowlist_merchant_ids",
        "denylist_merchant_ids",
        "sales_org_scope",
    ],
)
def test_empty_hard_rule_collections_fail_closed(field: str) -> None:
    with pytest.raises(ValidationError):
        _criteria(**{field: ()})


def test_new_domain_values_are_deeply_immutable_and_reject_non_finite_input() -> None:
    categories = ["餐饮"]
    criteria = _criteria(categories=categories)
    categories.append("零售")

    assert criteria.categories == ("餐饮",)
    with pytest.raises(ValidationError):
        _merchant(categories=(math.inf,))
