"""Deterministic product hard-eligibility values and policy."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypeAlias

from pydantic import Field, field_validator

from oria.core.types import JsonValue, ValueModel
from oria.rag.models import CampaignRuleSnapshot

ProductEligibilityReason: TypeAlias = Literal[
    "eligible",
    "price_below_minimum",
    "price_above_maximum",
    "category_mismatch",
    "keyword_mismatch",
    "product_unavailable",
]


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
