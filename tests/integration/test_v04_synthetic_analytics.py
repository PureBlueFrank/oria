"""V0.4-T01 deterministic synthetic analytics generation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oria.eval.attribution_data import (
    ATTRIBUTION_DATASET_VERSION,
    ATTRIBUTION_GENERATOR_SEED,
    generate_attribution_fixture,
)

pytestmark = pytest.mark.integration


def _rows(database: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2, 3, 4').fetchall()


def test_generator_is_deterministic_and_records_synthetic_provenance(tmp_path: Path) -> None:
    first_query = tmp_path / "first" / "analytics.db"
    first_labels = tmp_path / "first" / "labels.db"
    second_query = tmp_path / "second" / "analytics.db"
    second_labels = tmp_path / "second" / "labels.db"

    first = generate_attribution_fixture(first_query, first_labels)
    second = generate_attribution_fixture(second_query, second_labels)

    assert first == second
    assert first.generator_seed == ATTRIBUTION_GENERATOR_SEED
    assert first.fixture_variant == "standard"
    assert first.dataset_version == f"{ATTRIBUTION_DATASET_VERSION}:standard"
    assert first.funnel_fact_count == 180
    assert first.market_fact_count == 180
    for table in ("analytics_metadata", "funnel_daily", "activity_windows", "market_daily"):
        assert _rows(first_query, table) == _rows(second_query, table)
    metadata = _rows(first_query, "analytics_metadata")
    assert metadata == [
        (
            f"{ATTRIBUTION_DATASET_VERSION}:standard",
            2,
            "scenario_b_generator_v3",
            ATTRIBUTION_GENERATOR_SEED,
            "synthetic",
            0,
            "CC0-1.0",
            "2026-09-02T00:00:00+00:00",
        )
    ]


def test_seed_contains_a_local_anomaly_with_stable_market_context(tmp_path: Path) -> None:
    query_database = tmp_path / "analytics.db"
    generate_attribution_fixture(query_database, tmp_path / "labels.db")

    with sqlite3.connect(query_database) as connection:
        before, after = connection.execute(
            """
            SELECT event_date, CAST(redemptions AS REAL) / confirmations
            FROM funnel_daily
            WHERE tenant_id = 'local-community'
              AND region = 'east'
              AND category = 'full_service'
              AND event_date IN ('2026-08-30', '2026-08-31')
            ORDER BY event_date
            """
        ).fetchall()
        market = connection.execute(
            """
            SELECT MIN(CAST(market_redemptions AS REAL) / market_enrollments),
                   MAX(CAST(market_redemptions AS REAL) / market_enrollments)
            FROM market_daily
            WHERE tenant_id = 'local-community'
              AND region = 'east'
              AND category = 'full_service'
              AND event_date IN ('2026-08-30', '2026-08-31')
            """
        ).fetchone()
        activity_end = connection.execute(
            """
            SELECT ends_on FROM activity_windows
            WHERE tenant_id = 'local-community'
              AND region = 'east'
              AND category = 'full_service'
            """
        ).fetchone()

    assert before[0] == "2026-08-30" and after[0] == "2026-08-31"
    assert float(before[1]) > 0.65
    assert float(after[1]) < 0.37
    assert market is not None and float(market[1]) - float(market[0]) < 0.03
    assert activity_end == ("2026-08-30",)


@pytest.mark.parametrize(
    ("variant", "expectation"),
    [
        ("campaign_effect", "local_withdrawal"),
        ("market_conflict", "market_and_local"),
        ("mixed_funnel", "upstream_and_downstream"),
        ("overlapping_events", "two_local_events"),
        ("beverage_conflict", "two_beverage_events"),
        ("systemic_category", "category_wide"),
        ("upstream_drop", "visits_only"),
        ("campaign_enrollment", "enrollment_only"),
        ("campaign_confirmation", "confirmation_only"),
        ("missing_activity", "missing_activity"),
        ("no_anomaly", "stable"),
    ],
)
def test_fixture_variants_expose_distinct_observable_evidence(
    tmp_path: Path,
    variant: str,
    expectation: str,
) -> None:
    query_database = tmp_path / variant / "analytics.db"
    generate_attribution_fixture(
        query_database,
        tmp_path / variant / "labels.db",
        fixture_variant=variant,  # type: ignore[arg-type]
    )
    with sqlite3.connect(query_database) as connection:
        rows = connection.execute(
            """
            SELECT region, category, event_date,
                   CAST(visits AS REAL) / impressions,
                   CAST(enrollments AS REAL) / visits,
                   CAST(confirmations AS REAL) / enrollments,
                   CAST(redemptions AS REAL) / confirmations
            FROM funnel_daily
            WHERE tenant_id = 'local-community'
              AND event_date IN ('2026-08-30', '2026-08-31')
            ORDER BY region, category, event_date
            """
        ).fetchall()
        activities = connection.execute(
            """
            SELECT region, category, activity_type, ends_on
            FROM activity_windows
            WHERE tenant_id = 'local-community'
            ORDER BY activity_id
            """
        ).fetchall()
        market = connection.execute(
            """
            SELECT region, category, event_date,
                   CAST(market_redemptions AS REAL) / market_enrollments
            FROM market_daily
            WHERE tenant_id = 'local-community'
              AND event_date IN ('2026-08-30', '2026-08-31')
            ORDER BY region, category, event_date
            """
        ).fetchall()

    def funnel_pair(region: str, category: str) -> tuple[tuple[object, ...], ...]:
        return tuple(row for row in rows if row[0:2] == (region, category))

    def market_pair(region: str, category: str) -> tuple[float, float]:
        values = tuple(float(row[3]) for row in market if row[0:2] == (region, category))
        assert len(values) == 2
        return values

    east_full = funnel_pair("east", "full_service")
    north_full = funnel_pair("north", "full_service")
    if expectation == "local_withdrawal":
        assert float(east_full[0][6]) > 0.79 and float(east_full[1][6]) < 0.37
        assert abs(float(north_full[1][6]) - float(north_full[0][6])) < 0.04
        assert (
            abs(market_pair("east", "full_service")[1] - market_pair("east", "full_service")[0])
            < 0.03
        )
    elif expectation == "market_and_local":
        assert float(east_full[1][6]) < float(east_full[0][6]) - 0.25
        assert market_pair("east", "full_service")[1] < market_pair("east", "full_service")[0] - 0.2
    elif expectation == "upstream_and_downstream":
        assert float(east_full[1][3]) < float(east_full[0][3]) - 0.15
        assert float(east_full[1][6]) < float(east_full[0][6]) - 0.15
        assert abs(float(east_full[1][4]) - float(east_full[0][4])) < 0.04
    elif expectation == "two_local_events":
        matching = [row for row in activities if row[0:2] == ("east", "full_service")]
        assert len(matching) == 2 and {row[2] for row in matching} == {
            "merchant_incentive",
            "redemption_rule_pilot",
        }
    elif expectation == "two_beverage_events":
        east_beverage = funnel_pair("east", "beverage")
        assert float(east_beverage[1][6]) < float(east_beverage[0][6]) - 0.2
        matching = [row for row in activities if row[0:2] == ("east", "beverage")]
        assert len(matching) == 2
        assert market_pair("east", "beverage")[1] < market_pair("east", "beverage")[0] - 0.2
    elif expectation == "category_wide":
        assert float(east_full[1][6]) < float(east_full[0][6]) - 0.15
        assert float(north_full[1][6]) < float(north_full[0][6]) - 0.15
        assert market_pair("east", "full_service")[1] < market_pair("east", "full_service")[0] - 0.2
    elif expectation == "visits_only":
        assert float(east_full[1][3]) < float(east_full[0][3]) - 0.15
        assert abs(float(east_full[1][4]) - float(east_full[0][4])) < 0.04
        assert abs(float(east_full[1][6]) - float(east_full[0][6])) < 0.04
    elif expectation == "enrollment_only":
        assert float(east_full[1][4]) < float(east_full[0][4]) - 0.15
        assert abs(float(east_full[1][6]) - float(east_full[0][6])) < 0.04
    elif expectation == "confirmation_only":
        assert float(east_full[1][5]) < float(east_full[0][5]) - 0.15
        assert abs(float(east_full[1][6]) - float(east_full[0][6])) < 0.04
    elif expectation == "missing_activity":
        assert not [row for row in activities if row[0:2] == ("east", "full_service")]
        assert float(east_full[1][6]) < float(east_full[0][6]) - 0.25
    else:
        assert abs(float(east_full[1][6]) - float(east_full[0][6])) < 0.04
