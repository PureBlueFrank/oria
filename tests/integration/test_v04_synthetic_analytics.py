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
    assert first.funnel_fact_count == 180
    assert first.market_fact_count == 180
    for table in ("analytics_metadata", "funnel_daily", "activity_windows", "market_daily"):
        assert _rows(first_query, table) == _rows(second_query, table)
    metadata = _rows(first_query, "analytics_metadata")
    assert metadata == [
        (
            ATTRIBUTION_DATASET_VERSION,
            1,
            "scenario_b_generator_v1",
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
