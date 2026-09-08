"""Deterministic Scenario B fixture generation and eval-only labels."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field

from oria.analytics.models import ActivityFact, FunnelDailyFact, MarketDailyFact
from oria.analytics.schema import ANALYTICS_SCHEMA_VERSION, create_analytics_schema
from oria.core.types import ValueModel

ATTRIBUTION_DATASET_VERSION = "scenario_b_synthetic_v3"
ATTRIBUTION_GENERATOR_VERSION: Literal["scenario_b_generator_v3"] = "scenario_b_generator_v3"
ATTRIBUTION_GENERATOR_SEED = 20260902
_GENERATED_AT = datetime(2026, 9, 2, tzinfo=UTC)
_START_DATE = date(2026, 8, 18)
_DAY_COUNT = 15

AttributionFixtureVariant = Literal[
    "standard",
    "campaign_effect",
    "market_conflict",
    "mixed_funnel",
    "overlapping_events",
    "beverage_conflict",
    "systemic_category",
    "upstream_drop",
    "campaign_enrollment",
    "campaign_confirmation",
    "missing_activity",
    "no_anomaly",
    "north_campaign_effect",
    "north_market_conflict",
    "campaign_effect_alt",
    "campaign_effect_multi",
    "campaign_enrollment_alt",
    "market_conflict_alt",
    "mixed_funnel_alt",
    "systemic_category_alt",
]
ATTRIBUTION_FIXTURE_VARIANTS: frozenset[str] = frozenset(get_args(AttributionFixtureVariant))


class AttributionLabel(ValueModel):
    case_id: str = Field(pattern=r"^sb-seed-[0-9]{3}$")
    tenant_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_outcome: Literal["attributed", "conflicting", "insufficient"]
    root_cause_code: str = Field(min_length=1)
    acceptable_hypotheses: tuple[str, ...] = Field(min_length=1)
    required_evidence: tuple[str, ...] = Field(min_length=1)
    golden_rationale: str = Field(min_length=1)


class AttributionFixtureManifest(ValueModel):
    dataset_version: str = Field(min_length=1)
    schema_version: Literal[2] = 2
    generator_version: Literal["scenario_b_generator_v3"] = ATTRIBUTION_GENERATOR_VERSION
    fixture_variant: AttributionFixtureVariant
    generator_seed: int = Field(ge=0)
    source: Literal["synthetic"] = "synthetic"
    contains_real_entities: Literal[False] = False
    license: Literal["CC0-1.0"] = "CC0-1.0"
    generated_at: datetime = _GENERATED_AT
    funnel_fact_count: int = Field(gt=0)
    activity_fact_count: int = Field(gt=0)
    market_fact_count: int = Field(gt=0)
    label_count: int = Field(gt=0)


def _funnel_facts(
    seed: int,
    fixture_variant: AttributionFixtureVariant = "standard",
) -> tuple[FunnelDailyFact, ...]:
    rng = random.Random(seed)
    facts: list[FunnelDailyFact] = []
    for offset in range(_DAY_COUNT):
        event_date = _START_DATE + timedelta(days=offset)
        for tenant_id in ("local-community", "tenant-secondary"):
            for region in ("east", "north"):
                for category in ("full_service", "quick_service", "beverage"):
                    impressions = 1800 + rng.randint(-80, 80)
                    visit_rate = 0.58 + rng.uniform(-0.015, 0.015)
                    enrollment_rate = 0.48 + rng.uniform(-0.015, 0.015)
                    confirmation_rate = 0.76 + rng.uniform(-0.01, 0.01)
                    redemption_rate = 0.7 + rng.uniform(-0.015, 0.015)
                    is_local_east = tenant_id == "local-community" and region == "east"
                    is_local_north = tenant_id == "local-community" and region == "north"
                    is_post = event_date >= date(2026, 8, 31)
                    if is_local_east and category == "full_service":
                        if fixture_variant == "campaign_effect":
                            redemption_rate = (0.82 if not is_post else 0.34) + rng.uniform(
                                -0.01, 0.01
                            )
                        elif fixture_variant == "campaign_effect_alt":
                            redemption_rate = (0.79 if not is_post else 0.38) + rng.uniform(
                                -0.01, 0.01
                            )
                        elif fixture_variant == "campaign_effect_multi":
                            redemption_rate = (0.80 if not is_post else 0.41) + rng.uniform(
                                -0.01, 0.01
                            )
                        elif (
                            fixture_variant
                            in {
                                "standard",
                                "market_conflict",
                                "overlapping_events",
                                "missing_activity",
                            }
                            and is_post
                        ):
                            redemption_rate = 0.34 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "market_conflict_alt" and is_post:
                            redemption_rate = 0.36 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "mixed_funnel" and is_post:
                            visit_rate = 0.38 + rng.uniform(-0.01, 0.01)
                            redemption_rate = 0.45 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "mixed_funnel_alt" and is_post:
                            visit_rate = 0.40 + rng.uniform(-0.01, 0.01)
                            redemption_rate = 0.50 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "upstream_drop" and is_post:
                            visit_rate = 0.36 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "campaign_enrollment" and is_post:
                            enrollment_rate = 0.28 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "campaign_enrollment_alt" and is_post:
                            enrollment_rate = 0.30 + rng.uniform(-0.01, 0.01)
                        elif fixture_variant == "campaign_confirmation" and is_post:
                            confirmation_rate = 0.45 + rng.uniform(-0.01, 0.01)
                    if is_local_north and category == "full_service":
                        if fixture_variant == "north_campaign_effect":
                            redemption_rate = (0.74 if not is_post else 0.32) + rng.uniform(
                                -0.01, 0.01
                            )
                        elif fixture_variant == "north_market_conflict" and is_post:
                            redemption_rate = 0.32 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "systemic_category"
                        and tenant_id == "local-community"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.45 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "systemic_category_alt"
                        and tenant_id == "local-community"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.50 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "beverage_conflict"
                        and is_local_east
                        and category == "beverage"
                        and is_post
                    ):
                        redemption_rate = 0.4 + rng.uniform(-0.01, 0.01)
                    visits = int(impressions * visit_rate)
                    enrollments = int(visits * enrollment_rate)
                    confirmations = int(enrollments * confirmation_rate)
                    facts.append(
                        FunnelDailyFact(
                            tenant_id=tenant_id,
                            event_date=event_date,
                            region=region,
                            category=category,
                            impressions=impressions,
                            visits=visits,
                            enrollments=enrollments,
                            confirmations=confirmations,
                            redemptions=int(confirmations * redemption_rate),
                        )
                    )
    return tuple(facts)


def _activity_facts(
    fixture_variant: AttributionFixtureVariant = "standard",
) -> tuple[ActivityFact, ...]:
    facts = [
        ActivityFact(
            tenant_id="local-community",
            activity_id="activity-east-full-service-summer",
            region="east",
            category="full_service",
            activity_type="merchant_incentive",
            merchant_id="synthetic-merchant-east-full-service",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 30),
        ),
        ActivityFact(
            tenant_id="local-community",
            activity_id="activity-east-quick-service-always-on",
            region="east",
            category="quick_service",
            activity_type="always_on",
            merchant_id="synthetic-merchant-east-quick-service",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 9, 30),
        ),
        ActivityFact(
            tenant_id="tenant-secondary",
            activity_id="activity-secondary-baseline",
            region="east",
            category="full_service",
            activity_type="always_on",
            merchant_id="synthetic-merchant-secondary",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 9, 30),
        ),
    ]
    if fixture_variant == "missing_activity":
        facts = [
            fact
            for fact in facts
            if fact.category != "full_service" or fact.tenant_id != "local-community"
        ]
    if fixture_variant == "overlapping_events":
        facts.append(
            ActivityFact(
                tenant_id="local-community",
                activity_id="activity-east-full-service-rule-pilot",
                region="east",
                category="full_service",
                activity_type="redemption_rule_pilot",
                merchant_id="synthetic-merchant-east-full-service",
                starts_on=date(2026, 8, 20),
                ends_on=date(2026, 8, 30),
            )
        )
    if fixture_variant == "beverage_conflict":
        facts.extend(
            (
                ActivityFact(
                    tenant_id="local-community",
                    activity_id="activity-east-beverage-discount",
                    region="east",
                    category="beverage",
                    activity_type="consumer_discount",
                    merchant_id="synthetic-merchant-east-beverage",
                    starts_on=date(2026, 8, 10),
                    ends_on=date(2026, 8, 30),
                ),
                ActivityFact(
                    tenant_id="local-community",
                    activity_id="activity-east-beverage-validation-pilot",
                    region="east",
                    category="beverage",
                    activity_type="redemption_rule_pilot",
                    merchant_id="synthetic-merchant-east-beverage",
                    starts_on=date(2026, 8, 20),
                    ends_on=date(2026, 8, 30),
                ),
            )
        )
    if fixture_variant in {"north_campaign_effect", "north_market_conflict"}:
        facts.append(
            ActivityFact(
                tenant_id="local-community",
                activity_id="activity-north-full-service-summer",
                region="north",
                category="full_service",
                activity_type="merchant_incentive",
                merchant_id="synthetic-merchant-north-full-service",
                starts_on=date(2026, 8, 1),
                ends_on=date(2026, 8, 30),
            )
        )
    return tuple(facts)


def _market_facts(
    seed: int,
    fixture_variant: AttributionFixtureVariant = "standard",
) -> tuple[MarketDailyFact, ...]:
    rng = random.Random(seed ^ 0x5A5A)
    facts: list[MarketDailyFact] = []
    for offset in range(_DAY_COUNT):
        event_date = _START_DATE + timedelta(days=offset)
        for tenant_id in ("local-community", "tenant-secondary"):
            for region in ("east", "north"):
                for category in ("full_service", "quick_service", "beverage"):
                    enrollments = 1200 + rng.randint(-30, 30)
                    redemption_rate = 0.69 + rng.uniform(-0.01, 0.01)
                    is_post = event_date >= date(2026, 8, 31)
                    if (
                        fixture_variant == "market_conflict"
                        and tenant_id == "local-community"
                        and region == "east"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.45 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "market_conflict_alt"
                        and tenant_id == "local-community"
                        and region == "east"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.48 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "systemic_category"
                        and tenant_id == "local-community"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.45 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "systemic_category_alt"
                        and tenant_id == "local-community"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.50 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "beverage_conflict"
                        and tenant_id == "local-community"
                        and region == "east"
                        and category == "beverage"
                        and is_post
                    ):
                        redemption_rate = 0.46 + rng.uniform(-0.01, 0.01)
                    if (
                        fixture_variant == "north_market_conflict"
                        and tenant_id == "local-community"
                        and region == "north"
                        and category == "full_service"
                        and is_post
                    ):
                        redemption_rate = 0.42 + rng.uniform(-0.01, 0.01)
                    facts.append(
                        MarketDailyFact(
                            tenant_id=tenant_id,
                            event_date=event_date,
                            region=region,
                            category=category,
                            market_enrollments=enrollments,
                            market_redemptions=int(enrollments * redemption_rate),
                        )
                    )
    return tuple(facts)


def _labels() -> tuple[AttributionLabel, ...]:
    return (
        AttributionLabel(
            case_id="sb-seed-001",
            tenant_id="local-community",
            question="为什么 2026-08-31 华东正餐招商核销转化率明显下降?",
            expected_outcome="attributed",
            root_cause_code="full_service_campaign_ended",
            acceptable_hypotheses=("正餐激励活动结束导致短期核销下滑",),
            required_evidence=(
                "华东正餐核销转化在 2026-08-31 出现结构性下降",
                "华东正餐激励活动于 2026-08-30 结束",
                "同期大盘核销转化保持稳定",
            ),
            golden_rationale="区域下钻异常与活动结束时间相邻, 而同期大盘没有同幅下降。",
        ),
    )


def _create_label_schema(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE attribution_labels (
                case_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                question TEXT NOT NULL,
                expected_outcome TEXT NOT NULL,
                root_cause_code TEXT NOT NULL,
                acceptable_hypotheses_json TEXT NOT NULL,
                required_evidence_json TEXT NOT NULL,
                golden_rationale TEXT NOT NULL
            );
            """
        )


def generate_attribution_fixture(
    query_database: Path,
    label_database: Path,
    *,
    seed: int = ATTRIBUTION_GENERATOR_SEED,
    fixture_variant: AttributionFixtureVariant = "standard",
) -> AttributionFixtureManifest:
    """Create deterministic query facts and physically separate eval labels."""
    if seed < 0:
        raise ValueError("generator seed must be non-negative")
    if query_database.resolve() == label_database.resolve():
        raise ValueError("query facts and evaluation labels require different databases")
    if query_database.exists() or label_database.exists():
        raise FileExistsError("attribution fixture output already exists")

    funnel_facts = _funnel_facts(seed, fixture_variant)
    activity_facts = _activity_facts(fixture_variant)
    market_facts = _market_facts(seed, fixture_variant)
    labels = _labels()

    create_analytics_schema(query_database)
    label_database.parent.mkdir(parents=True, exist_ok=True)
    _create_label_schema(label_database)

    with sqlite3.connect(query_database) as connection:
        connection.execute(
            "INSERT INTO analytics_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{ATTRIBUTION_DATASET_VERSION}:{fixture_variant}",
                ANALYTICS_SCHEMA_VERSION,
                ATTRIBUTION_GENERATOR_VERSION,
                seed,
                "synthetic",
                0,
                "CC0-1.0",
                _GENERATED_AT.isoformat(),
            ),
        )
        connection.executemany(
            "INSERT INTO funnel_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    fact.tenant_id,
                    fact.event_date.isoformat(),
                    fact.region,
                    fact.category,
                    fact.impressions,
                    fact.visits,
                    fact.enrollments,
                    fact.confirmations,
                    fact.redemptions,
                )
                for fact in funnel_facts
            ),
        )
        connection.executemany(
            "INSERT INTO activity_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    fact.tenant_id,
                    fact.activity_id,
                    fact.region,
                    fact.category,
                    fact.activity_type,
                    fact.merchant_id,
                    fact.starts_on.isoformat(),
                    fact.ends_on.isoformat(),
                )
                for fact in activity_facts
            ),
        )
        connection.executemany(
            "INSERT INTO market_daily VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    fact.tenant_id,
                    fact.event_date.isoformat(),
                    fact.region,
                    fact.category,
                    fact.market_enrollments,
                    fact.market_redemptions,
                )
                for fact in market_facts
            ),
        )

    with sqlite3.connect(label_database) as connection:
        connection.executemany(
            "INSERT INTO attribution_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    label.case_id,
                    label.tenant_id,
                    label.question,
                    label.expected_outcome,
                    label.root_cause_code,
                    json.dumps(label.acceptable_hypotheses, ensure_ascii=False),
                    json.dumps(label.required_evidence, ensure_ascii=False),
                    label.golden_rationale,
                )
                for label in labels
            ),
        )

    return AttributionFixtureManifest(
        dataset_version=f"{ATTRIBUTION_DATASET_VERSION}:{fixture_variant}",
        fixture_variant=fixture_variant,
        generator_seed=seed,
        funnel_fact_count=len(funnel_facts),
        activity_fact_count=len(activity_facts),
        market_fact_count=len(market_facts),
        label_count=len(labels),
    )
