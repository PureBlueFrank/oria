"""Read-only, tenant-scoped queries over Scenario B analytics facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from oria.analytics.models import (
    ActivityWindow,
    AnalyticsEvidence,
    AnalyticsPeriod,
    FunnelMetrics,
    FunnelPoint,
    MarketMetrics,
    MarketSegmentOverview,
)
from oria.analytics.schema import ANALYTICS_SCHEMA_VERSION

FunnelDimension = Literal["event_date", "region", "category"]
MarketDimension = Literal["region", "category"]
ComparisonKind = Literal["previous_period", "year_over_year"]

_FUNNEL_DIMENSIONS = frozenset({"event_date", "region", "category"})
_MARKET_DIMENSIONS = frozenset({"region", "category"})
_REQUIRED_COLUMNS = {
    "analytics_metadata": frozenset({"dataset_version", "schema_version"}),
    "funnel_daily": frozenset(
        {
            "tenant_id",
            "event_date",
            "region",
            "category",
            "impressions",
            "visits",
            "enrollments",
            "confirmations",
            "redemptions",
        }
    ),
    "activity_windows": frozenset(
        {
            "tenant_id",
            "activity_id",
            "region",
            "category",
            "activity_type",
            "merchant_id",
            "starts_on",
            "ends_on",
        }
    ),
    "market_daily": frozenset(
        {
            "tenant_id",
            "event_date",
            "region",
            "category",
            "market_enrollments",
            "market_redemptions",
        }
    ),
}


class AnalyticsQueryError(RuntimeError):
    """Raised when the trusted analytics database is unavailable or incompatible."""


class AnalyticsQueryStore:
    """Execute fixed, parameterized SELECT statements against a read-only database."""

    def __init__(self, database: Path) -> None:
        self._database = database.resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self._database.is_file():
            raise AnalyticsQueryError("analytics database is unavailable")
        uri = f"file:{quote(str(self._database), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            self._validate_schema(connection)
            yield connection
        except sqlite3.Error as exc:
            raise AnalyticsQueryError("analytics query failed closed") from exc
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != ANALYTICS_SCHEMA_VERSION:
            raise AnalyticsQueryError("analytics schema version is incompatible")
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not required.issubset(columns):
                raise AnalyticsQueryError("analytics schema is incomplete")

    @staticmethod
    def _dataset_version(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT dataset_version FROM analytics_metadata ORDER BY dataset_version"
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise AnalyticsQueryError("analytics metadata is invalid")
        return str(rows[0][0])

    def query_funnel(
        self,
        *,
        tenant_id: str,
        period: AnalyticsPeriod,
        dimensions: Sequence[FunnelDimension],
        region: str | None = None,
        category: str | None = None,
    ) -> tuple[tuple[FunnelPoint, ...], AnalyticsEvidence]:
        selected = _validate_dimensions(dimensions, _FUNNEL_DIMENSIONS)
        where, params, filters = _scope(
            tenant_id=tenant_id,
            period=period,
            region=region,
            category=category,
        )
        group_sql = ", ".join(selected)
        select_sql = f"{group_sql}, " if group_sql else ""
        group_clause = f" GROUP BY {group_sql} ORDER BY {group_sql}" if group_sql else ""
        statement = (
            f"SELECT {select_sql}SUM(impressions) AS impressions, SUM(visits) AS visits, "
            "SUM(enrollments) AS enrollments, SUM(confirmations) AS confirmations, "
            f"SUM(redemptions) AS redemptions FROM funnel_daily WHERE {where}{group_clause}"
        )
        with self._connection() as connection:
            dataset_version = self._dataset_version(connection)
            rows = connection.execute(statement, params).fetchall()
        points = tuple(_funnel_point(row, selected) for row in rows)
        evidence = _evidence(
            dataset_version=dataset_version,
            source_tables=("funnel_daily",),
            tenant_id=tenant_id,
            period=period,
            dimensions=selected,
            filters=filters,
            row_count=len(points),
        )
        return points, evidence

    def drill_down(
        self,
        *,
        tenant_id: str,
        period: AnalyticsPeriod,
        dimension: Literal["region", "category"],
        value: str,
        group_by: Sequence[FunnelDimension],
    ) -> tuple[tuple[FunnelPoint, ...], AnalyticsEvidence]:
        if dimension not in _MARKET_DIMENSIONS:
            raise ValueError("drill-down dimension is not allowed")
        selected = _validate_dimensions(group_by, _FUNNEL_DIMENSIONS)
        if dimension in selected:
            raise ValueError("drill-down dimension cannot also be grouped")
        return self.query_funnel(
            tenant_id=tenant_id,
            period=period,
            dimensions=cast(tuple[FunnelDimension, ...], selected),
            region=value if dimension == "region" else None,
            category=value if dimension == "category" else None,
        )

    def query_activity(
        self,
        *,
        tenant_id: str,
        period: AnalyticsPeriod,
        category: str | None,
        merchant_id: str | None,
    ) -> tuple[tuple[ActivityWindow, ...], AnalyticsEvidence]:
        if category is None and merchant_id is None:
            raise ValueError("category or merchant_id is required")
        predicates = ["tenant_id = ?", "starts_on <= ?", "ends_on >= ?"]
        params: list[str] = [tenant_id, period.end_date.isoformat(), period.start_date.isoformat()]
        filters: dict[str, str] = {}
        if category is not None:
            predicates.append("category = ?")
            params.append(category)
            filters["category"] = category
        if merchant_id is not None:
            predicates.append("merchant_id = ?")
            params.append(merchant_id)
            filters["merchant_id"] = merchant_id
        statement = (
            "SELECT activity_id, region, category, activity_type, merchant_id, starts_on, ends_on "
            f"FROM activity_windows WHERE {' AND '.join(predicates)} "
            "ORDER BY starts_on, activity_id"
        )
        with self._connection() as connection:
            dataset_version = self._dataset_version(connection)
            rows = connection.execute(statement, params).fetchall()
        windows = tuple(
            ActivityWindow(
                activity_id=str(row["activity_id"]),
                region=str(row["region"]),
                category=str(row["category"]),
                activity_type=str(row["activity_type"]),
                merchant_id=(str(row["merchant_id"]) if row["merchant_id"] is not None else None),
                starts_on=date.fromisoformat(str(row["starts_on"])),
                ends_on=date.fromisoformat(str(row["ends_on"])),
            )
            for row in rows
        )
        return windows, _evidence(
            dataset_version=dataset_version,
            source_tables=("activity_windows",),
            tenant_id=tenant_id,
            period=period,
            dimensions=(),
            filters=filters,
            row_count=len(windows),
        )

    def query_market_overview(
        self,
        *,
        tenant_id: str,
        period: AnalyticsPeriod,
        comparison: ComparisonKind,
        dimensions: Sequence[MarketDimension],
        region: str | None = None,
        category: str | None = None,
    ) -> tuple[tuple[MarketSegmentOverview, ...], AnalyticsEvidence]:
        selected = _validate_dimensions(dimensions, _MARKET_DIMENSIONS)
        comparison_period = _comparison_period(period, comparison)
        with self._connection() as connection:
            dataset_version = self._dataset_version(connection)
            current = _market_rows(
                connection,
                tenant_id=tenant_id,
                period=period,
                dimensions=cast(tuple[MarketDimension, ...], selected),
                region=region,
                category=category,
            )
            previous = _market_rows(
                connection,
                tenant_id=tenant_id,
                period=comparison_period,
                dimensions=cast(tuple[MarketDimension, ...], selected),
                region=region,
                category=category,
            )
        previous_by_key = {_row_key(row, selected): row for row in previous}
        results: list[MarketSegmentOverview] = []
        for row in current:
            earlier = previous_by_key.get(_row_key(row, selected))
            current_metrics = _market_metrics(row)
            comparison_metrics = _market_metrics(earlier) if earlier is not None else None
            results.append(
                MarketSegmentOverview(
                    region=str(row["region"]) if "region" in selected else None,
                    category=str(row["category"]) if "category" in selected else None,
                    current=current_metrics,
                    comparison=comparison_metrics,
                    redemption_rate_change=(
                        round(
                            current_metrics.redemption_rate - comparison_metrics.redemption_rate,
                            6,
                        )
                        if comparison_metrics is not None
                        else None
                    ),
                )
            )
        filters = {
            "comparison": comparison,
            "comparison_start_date": comparison_period.start_date.isoformat(),
            "comparison_end_date": comparison_period.end_date.isoformat(),
        }
        if region is not None:
            filters["region"] = region
        if category is not None:
            filters["category"] = category
        return tuple(results), _evidence(
            dataset_version=dataset_version,
            source_tables=("market_daily",),
            tenant_id=tenant_id,
            period=period,
            dimensions=selected,
            filters=filters,
            row_count=len(results),
        )


def _validate_dimensions(dimensions: Sequence[str], allowed: frozenset[str]) -> tuple[str, ...]:
    selected = tuple(dimensions)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("analytics dimensions must be non-empty and unique")
    if any(dimension not in allowed for dimension in selected):
        raise ValueError("analytics dimension is not allowed")
    return selected


def _scope(
    *,
    tenant_id: str,
    period: AnalyticsPeriod,
    region: str | None,
    category: str | None,
) -> tuple[str, list[str], dict[str, str]]:
    predicates = ["tenant_id = ?", "event_date BETWEEN ? AND ?"]
    params = [tenant_id, period.start_date.isoformat(), period.end_date.isoformat()]
    filters: dict[str, str] = {}
    if region is not None:
        predicates.append("region = ?")
        params.append(region)
        filters["region"] = region
    if category is not None:
        predicates.append("category = ?")
        params.append(category)
        filters["category"] = category
    return " AND ".join(predicates), params, filters


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _funnel_point(row: sqlite3.Row, dimensions: Sequence[str]) -> FunnelPoint:
    impressions = int(row["impressions"] or 0)
    visits = int(row["visits"] or 0)
    enrollments = int(row["enrollments"] or 0)
    confirmations = int(row["confirmations"] or 0)
    redemptions = int(row["redemptions"] or 0)
    return FunnelPoint(
        event_date=(
            date.fromisoformat(str(row["event_date"])) if "event_date" in dimensions else None
        ),
        region=str(row["region"]) if "region" in dimensions else None,
        category=str(row["category"]) if "category" in dimensions else None,
        metrics=FunnelMetrics(
            impressions=impressions,
            visits=visits,
            enrollments=enrollments,
            confirmations=confirmations,
            redemptions=redemptions,
            visit_rate=_rate(visits, impressions),
            enrollment_rate=_rate(enrollments, visits),
            confirmation_rate=_rate(confirmations, enrollments),
            redemption_rate=_rate(redemptions, confirmations),
        ),
    )


def _market_rows(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    period: AnalyticsPeriod,
    dimensions: Sequence[MarketDimension],
    region: str | None,
    category: str | None,
) -> list[sqlite3.Row]:
    where, params, _ = _scope(
        tenant_id=tenant_id,
        period=period,
        region=region,
        category=category,
    )
    group_sql = ", ".join(dimensions)
    select_sql = f"{group_sql}, " if group_sql else ""
    group_clause = f" GROUP BY {group_sql} ORDER BY {group_sql}" if group_sql else ""
    return connection.execute(
        f"SELECT {select_sql}SUM(market_enrollments) AS enrollments, "
        f"SUM(market_redemptions) AS redemptions FROM market_daily WHERE {where}{group_clause}",
        params,
    ).fetchall()


def _market_metrics(row: sqlite3.Row) -> MarketMetrics:
    enrollments = int(row["enrollments"] or 0)
    redemptions = int(row["redemptions"] or 0)
    return MarketMetrics(
        enrollments=enrollments,
        redemptions=redemptions,
        redemption_rate=_rate(redemptions, enrollments),
    )


def _row_key(row: sqlite3.Row, dimensions: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(row[dimension]) for dimension in dimensions)


def _comparison_period(period: AnalyticsPeriod, comparison: ComparisonKind) -> AnalyticsPeriod:
    if comparison == "previous_period":
        duration = period.end_date - period.start_date
        return AnalyticsPeriod(
            start_date=period.start_date - duration - timedelta(days=1),
            end_date=period.start_date - timedelta(days=1),
        )
    if comparison != "year_over_year":
        raise ValueError("market comparison is not allowed")
    return AnalyticsPeriod(
        start_date=_previous_year(period.start_date),
        end_date=_previous_year(period.end_date),
    )


def _previous_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _evidence(
    *,
    dataset_version: str,
    source_tables: tuple[Literal["funnel_daily", "activity_windows", "market_daily"], ...],
    tenant_id: str,
    period: AnalyticsPeriod,
    dimensions: Sequence[str],
    filters: dict[str, str],
    row_count: int,
) -> AnalyticsEvidence:
    payload = {
        "dataset_version": dataset_version,
        "source_tables": source_tables,
        "tenant_id": tenant_id,
        "period": period.model_dump(mode="json"),
        "dimensions": tuple(dimensions),
        "filters": filters,
        "row_count": row_count,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    evidence_id = f"ae_{digest[:32]}"
    return AnalyticsEvidence(
        evidence_id=evidence_id,
        dataset_version=dataset_version,
        source_tables=source_tables,
        period=period,
        dimensions=cast(tuple[FunnelDimension, ...], tuple(dimensions)),
        filters=filters,
        row_count=row_count,
        provenance=f"oria://analytics/{dataset_version}/{evidence_id}",
    )
