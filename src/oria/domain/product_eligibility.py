"""Deterministic product hard-eligibility values and policy."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypeAlias

from pydantic import Field, field_validator

from oria.core.types import JsonValue, ValueModel
from oria.domain.models import EligibilityCriteria
from oria.rag.models import CampaignRuleSnapshot

ProductEligibilityReason: TypeAlias = Literal[
    "eligible",
    "price_below_minimum",
    "price_above_maximum",
    "category_mismatch",
    "keyword_mismatch",
    "product_unavailable",
]


def _value_hash(value: ValueModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class ProductSnapshot(ValueModel):
    """Complete trusted catalog value; distinct from the redacted Business entity."""

    product_ref: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1, repr=False)
    source_ref: str = Field(min_length=1)
    captured_at: datetime
    category: str = Field(min_length=1)
    normalized_price: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    normalized_title: str = Field(min_length=1)
    keyword_labels: tuple[str, ...]
    eligibility_facts: dict[str, JsonValue] = Field(repr=False)

    @field_validator("captured_at")
    @classmethod
    def require_aware_capture_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        return value

    @field_validator("normalized_price")
    @classmethod
    def normalize_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("normalized_price must be finite")
        return value.normalize()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("keyword_labels")
    @classmethod
    def normalize_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({item.strip().casefold() for item in value if item.strip()}))
        return normalized


class ProductEligibilityCriteria(ValueModel):
    """Frozen criteria copied only from an integrity-valid rule snapshot."""

    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_ref: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    price_min: Decimal = Field(ge=0)
    price_max: Decimal = Field(gt=0)
    categories: tuple[str, ...] = Field(min_length=1)
    keywords: tuple[str, ...] = Field(min_length=1)

    @classmethod
    def from_snapshot(cls, snapshot: CampaignRuleSnapshot) -> ProductEligibilityCriteria:
        if snapshot.recompute_hash() != snapshot.snapshot_hash:
            raise ValueError("campaign rule snapshot integrity verification failed")
        policy = snapshot.enrollment_policy
        return cls(
            rule_snapshot_id=snapshot.snapshot_id,
            rule_snapshot_hash=snapshot.snapshot_hash,
            policy_ref=policy.product_circle_policy_ref,
            policy_version=policy.product_circle_policy_version,
            price_min=policy.product_price_min,
            price_max=policy.product_price_max,
            categories=tuple(sorted(set(policy.product_categories))),
            keywords=tuple(
                sorted({keyword.strip().casefold() for keyword in policy.product_keywords})
            ),
        )


class EnrollmentEligibilityAttestation(ValueModel):
    """Frozen rule/policy hashes carried into the final Business transaction."""

    campaign_id: str = Field(min_length=1)
    rule_snapshot_ref_id: str = Field(min_length=1)
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    product_policy_ref: str = Field(min_length=1)
    product_policy_version: str = Field(min_length=1)
    catalog_snapshot_id: str = Field(min_length=1)
    merchant_criteria_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    product_criteria_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    item_business_keys_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        catalog_snapshot_id: str,
        merchant_criteria: EligibilityCriteria,
        product_criteria: ProductEligibilityCriteria,
        item_business_keys_hash: str,
    ) -> EnrollmentEligibilityAttestation:
        return cls(
            campaign_id=campaign_id,
            rule_snapshot_ref_id=rule_snapshot_ref_id,
            rule_snapshot_hash=product_criteria.rule_snapshot_hash,
            product_policy_ref=product_criteria.policy_ref,
            product_policy_version=product_criteria.policy_version,
            catalog_snapshot_id=catalog_snapshot_id,
            merchant_criteria_hash=_value_hash(merchant_criteria),
            product_criteria_hash=_value_hash(product_criteria),
            item_business_keys_hash=item_business_keys_hash,
        )

    def verify(
        self,
        *,
        merchant_criteria: EligibilityCriteria,
        product_criteria: ProductEligibilityCriteria,
        item_business_keys_hash: str,
    ) -> None:
        if (
            self.rule_snapshot_hash != product_criteria.rule_snapshot_hash
            or self.product_policy_ref != product_criteria.policy_ref
            or self.product_policy_version != product_criteria.policy_version
            or self.merchant_criteria_hash != _value_hash(merchant_criteria)
            or self.product_criteria_hash != _value_hash(product_criteria)
            or self.item_business_keys_hash != item_business_keys_hash
        ):
            raise ValueError("enrollment eligibility attestation does not match frozen criteria")


class ProductSellabilityAttestation(ValueModel):
    """Versioned current-catalog observation, separate from frozen enrollment eligibility."""

    catalog_snapshot_id: str = Field(min_length=1)
    product_states_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        catalog_snapshot_id: str,
        products: tuple[ProductSnapshot, ...],
    ) -> ProductSellabilityAttestation:
        ordered = tuple(
            sorted(
                products,
                key=lambda item: (item.merchant_id, item.product_ref, item.product_version),
            )
        )
        payload = json.dumps(
            [product.model_dump(mode="json") for product in ordered],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            catalog_snapshot_id=catalog_snapshot_id,
            product_states_hash=f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}",
        )

    def verify(self, products: tuple[ProductSnapshot, ...]) -> None:
        expected = self.create(
            catalog_snapshot_id=self.catalog_snapshot_id,
            products=products,
        )
        if expected != self:
            raise ValueError("current product sellability attestation does not match products")


class ProductEligibilityDecision(ValueModel):
    eligible: bool
    reason_codes: tuple[ProductEligibilityReason, ...] = Field(min_length=1)


class ProductEligibilityPolicy:
    """Apply all frozen price/category/keyword/status conditions without an LLM."""

    def evaluate(
        self,
        product: ProductSnapshot,
        criteria: ProductEligibilityCriteria,
    ) -> ProductEligibilityDecision:
        reasons: list[ProductEligibilityReason] = []
        if product.normalized_price < criteria.price_min:
            reasons.append("price_below_minimum")
        if product.normalized_price > criteria.price_max:
            reasons.append("price_above_maximum")
        if product.category not in criteria.categories:
            reasons.append("category_mismatch")
        if not set(product.keyword_labels).intersection(criteria.keywords):
            reasons.append("keyword_mismatch")
        facts = product.eligibility_facts
        if facts.get("status") != "available" or facts.get("available") is not True:
            reasons.append("product_unavailable")
        if reasons:
            return ProductEligibilityDecision(eligible=False, reason_codes=tuple(reasons))
        return ProductEligibilityDecision(eligible=True, reason_codes=("eligible",))

    def evaluate_page(
        self,
        products: tuple[ProductSnapshot, ...],
        criteria: ProductEligibilityCriteria,
    ) -> tuple[
        tuple[ProductSnapshot, ...],
        dict[ProductEligibilityReason, int],
    ]:
        eligible: list[ProductSnapshot] = []
        reasons: Counter[ProductEligibilityReason] = Counter()
        for product in products:
            decision = self.evaluate(product, criteria)
            if decision.eligible:
                eligible.append(product)
            else:
                reasons.update(decision.reason_codes)
        return tuple(eligible), dict(sorted(reasons.items()))
