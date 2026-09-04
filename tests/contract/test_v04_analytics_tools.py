"""V0.4-T02 contracts for tenant-scoped read-only attribution tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from oria.analytics.demo import attribution_history_document
from oria.analytics.query import AnalyticsQueryStore
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.eval.attribution_data import ATTRIBUTION_DATASET_VERSION, generate_attribution_fixture
from oria.permission.local import local_cli_executor, local_operator
from oria.tools.analytics import (
    DrillDownResult,
    QueryActivityResult,
    QueryFunnelResult,
    QueryMarketOverviewResult,
    SearchHistoryExperienceResult,
    build_attribution_tool_registry,
)

pytestmark = pytest.mark.contract

_TOOLS = {
    "query_funnel",
    "drill_down",
    "query_activity",
    "query_market_overview",
    "search_history_experience",
}


async def _setup(tmp_path: Path):
    query_database = tmp_path / "scenario-b" / "analytics.db"
    generate_attribution_fixture(query_database, tmp_path / "evaluation-only" / "labels.db")
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="v04-tools-session",
        thread_id="v04-tools-thread",
        run_id="v04-tools-run",
    )
    await runtime.knowledge.ingest(attribution_history_document(), ctx)
    registry = build_attribution_tool_registry(
        AnalyticsQueryStore(query_database), runtime.retriever
    )
    return runtime, ctx, registry


@pytest.mark.asyncio
async def test_registry_exposes_five_strict_read_only_tools(tmp_path: Path) -> None:
    runtime, _, registry = await _setup(tmp_path)
    try:
        assert set(registry) == _TOOLS
        assert registry.allowlist == frozenset(_TOOLS)
        for name in _TOOLS:
            tool = registry.get(name)
            assert tool.schema_version == 1
            assert tool.policy.side_effect is False
            assert tool.policy.approval_mode == "none"
            assert tool.json_schema["additionalProperties"] is False
            assert tool.result_schema["additionalProperties"] is False
            assert "tenant_id" not in tool.json_schema["properties"]
            assert "sql" not in tool.json_schema["properties"]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_tools_return_bounded_metrics_activities_market_and_citations(tmp_path: Path) -> None:
    runtime, ctx, registry = await _setup(tmp_path)
    try:
        period = {"start_date": "2026-08-30", "end_date": "2026-08-31"}
        funnel = QueryFunnelResult.model_validate(
            (
                await registry.execute(
                    "query_funnel",
                    {
                        "period": period,
                        "dimensions": ["event_date"],
                        "region": "east",
                        "category": "full_service",
                    },
                    ctx,
                )
            ).data
        )
        assert len(funnel.rows) == 2
        assert funnel.rows[0].metrics.redemption_rate > 0.65
        assert funnel.rows[1].metrics.redemption_rate < 0.37
        assert funnel.evidence.dataset_version == ATTRIBUTION_DATASET_VERSION
        assert funnel.evidence.source_tables == ("funnel_daily",)

        drill_down = DrillDownResult.model_validate(
            (
                await registry.execute(
                    "drill_down",
                    {
                        "period": period,
                        "dimension": "region",
                        "value": "east",
                        "group_by": ["event_date", "category"],
                    },
                    ctx,
                )
            ).data
        )
        assert len(drill_down.rows) == 6
        assert {row.region for row in drill_down.rows} == {None}
        assert drill_down.evidence.filters == {"region": "east"}

        activities = QueryActivityResult.model_validate(
            (
                await registry.execute(
                    "query_activity",
                    {
                        "period": {"start_date": "2026-08-29", "end_date": "2026-09-01"},
                        "merchant_id": "synthetic-merchant-east-full-service",
                    },
                    ctx,
                )
            ).data
        )
        assert [item.activity_id for item in activities.activities] == [
            "activity-east-full-service-summer"
        ]

        market = QueryMarketOverviewResult.model_validate(
            (
                await registry.execute(
                    "query_market_overview",
                    {
                        "period": {"start_date": "2026-08-31", "end_date": "2026-09-01"},
                        "comparison": "previous_period",
                        "dimensions": ["region", "category"],
                        "region": "east",
                        "category": "full_service",
                    },
                    ctx,
                )
            ).data
        )
        assert len(market.segments) == 1
        assert market.segments[0].comparison is not None
        assert abs(market.segments[0].redemption_rate_change or 0) < 0.03

        history_result = await registry.execute(
            "search_history_experience",
            {"query": "如何排查招商转化异常", "limit": 3},
            ctx,
        )
        history = SearchHistoryExperienceResult.model_validate(history_result.data)
        assert history_result.trust_level == "untrusted_data"
        assert len(history.hits) == 1
        assert history.hits[0].citation.document_id == "synthetic-attribution-history"
        assert await runtime.knowledge.citation_exists(history.hits[0].citation, ctx)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_tool_contracts_reject_unbounded_or_caller_owned_scope(tmp_path: Path) -> None:
    runtime, ctx, registry = await _setup(tmp_path)
    try:
        valid = {
            "period": {"start_date": "2026-08-30", "end_date": "2026-08-31"},
            "dimensions": ["event_date"],
        }
        with pytest.raises((JsonSchemaValidationError, PydanticValidationError)):
            await registry.execute("query_funnel", {**valid, "tenant_id": "tenant-secondary"}, ctx)
        with pytest.raises((JsonSchemaValidationError, PydanticValidationError)):
            await registry.execute(
                "query_funnel", {**valid, "sql": "SELECT * FROM funnel_daily"}, ctx
            )
        with pytest.raises((JsonSchemaValidationError, PydanticValidationError)):
            await registry.execute(
                "query_funnel",
                {
                    "period": {"start_date": "2020-01-01", "end_date": "2026-08-31"},
                    "dimensions": ["event_date"],
                },
                ctx,
            )
        with pytest.raises((JsonSchemaValidationError, PydanticValidationError)):
            await registry.execute(
                "query_activity",
                {"period": valid["period"]},
                ctx,
            )
    finally:
        await runtime.aclose()
