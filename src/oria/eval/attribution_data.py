"""Deterministic Scenario B fixture generation and eval-only labels."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from oria.analytics.models import ActivityFact, FunnelDailyFact, MarketDailyFact
from oria.analytics.schema import ANALYTICS_SCHEMA_VERSION, create_analytics_schema
from oria.core.types import ValueModel

ATTRIBUTION_DATASET_VERSION = "scenario_b_synthetic_v2"
ATTRIBUTION_GENERATOR_VERSION = "scenario_b_generator_v2"
ATTRIBUTION_GENERATOR_SEED = 20260902
_GENERATED_AT = datetime(2026, 9, 2, tzinfo=UTC)
_START_DATE = date(2026, 8, 18)
_DAY_COUNT = 15


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
    dataset_version: Literal["scenario_b_synthetic_v2"] = "scenario_b_synthetic_v2"
    schema_version: Literal[2] = 2
    generator_version: Literal["scenario_b_generator_v2"] = "scenario_b_generator_v2"
    generator_seed: int = Field(ge=0)
    source: Literal["synthetic"] = "synthetic"
    contains_real_entities: Literal[False] = False
    license: Literal["CC0-1.0"] = "CC0-1.0"
    generated_at: datetime = _GENERATED_AT
    funnel_fact_count: int = Field(gt=0)
    activity_fact_count: int = Field(gt=0)
    market_fact_count: int = Field(gt=0)
    label_count: int = Field(gt=0)


def _funnel_facts(seed: int) -> tuple[FunnelDailyFact, ...]:
    rng = random.Random(seed)
    facts: list[FunnelDailyFact] = []
    for offset in range(_DAY_COUNT):
        event_date = _START_DATE + timedelta(days=offset)
        for tenant_id in ("local-community", "tenant-secondary"):
            for region in ("east", "north"):
                for category in ("full_service", "quick_service", "beverage"):
                    impressions = 1800 + rng.randint(-80, 80)
                    visits = int(impressions * (0.58 + rng.uniform(-0.015, 0.015)))
                    enrollments = int(visits * (0.48 + rng.uniform(-0.015, 0.015)))
                    confirmations = int(enrollments * (0.76 + rng.uniform(-0.01, 0.01)))
                    redemption_rate = 0.7 + rng.uniform(-0.015, 0.015)
                    if (
                        tenant_id == "local-community"
                        and region == "east"
                        and category == "full_service"
                        and event_date >= date(2026, 8, 31)
                    ):
                        redemption_rate = 0.34 + rng.uniform(-0.01, 0.01)
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


def _activity_facts() -> tuple[ActivityFact, ...]:
    return (
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
    )


def _market_facts(seed: int) -> tuple[MarketDailyFact, ...]:
    rng = random.Random(seed ^ 0x5A5A)
    facts: list[MarketDailyFact] = []
    for offset in range(_DAY_COUNT):
        event_date = _START_DATE + timedelta(days=offset)
        for tenant_id in ("local-community", "tenant-secondary"):
            for region in ("east", "north"):
                for category in ("full_service", "quick_service", "beverage"):
                    enrollments = 1200 + rng.randint(-30, 30)
                    facts.append(
                        MarketDailyFact(
                            tenant_id=tenant_id,
                            event_date=event_date,
                            region=region,
                            category=category,
                            market_enrollments=enrollments,
                            market_redemptions=int(enrollments * (0.69 + rng.uniform(-0.01, 0.01))),
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
) -> AttributionFixtureManifest:
    """Create deterministic query facts and physically separate eval labels."""
    if seed < 0:
        raise ValueError("generator seed must be non-negative")
    if query_database.resolve() == label_database.resolve():
        raise ValueError("query facts and evaluation labels require different databases")
    if query_database.exists() or label_database.exists():
        raise FileExistsError("attribution fixture output already exists")

    funnel_facts = _funnel_facts(seed)
    activity_facts = _activity_facts()
    market_facts = _market_facts(seed)
    labels = _labels()

    create_analytics_schema(query_database)
    label_database.parent.mkdir(parents=True, exist_ok=True)
    _create_label_schema(label_database)

    with sqlite3.connect(query_database) as connection:
        connection.execute(
            "INSERT INTO analytics_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ATTRIBUTION_DATASET_VERSION,
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
        generator_seed=seed,
        funnel_fact_count=len(funnel_facts),
        activity_fact_count=len(activity_facts),
        market_fact_count=len(market_facts),
        label_count=len(labels),
    )
