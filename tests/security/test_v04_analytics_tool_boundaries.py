"""Security boundaries for V0.4-T02 analytics and history tools."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from oria.analytics.query import AnalyticsQueryError, AnalyticsQueryStore
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import Principal
from oria.data import initialize_data
from oria.eval.attribution_data import generate_attribution_fixture
from oria.permission.local import local_cli_executor, local_operator
from oria.tools.analytics import QueryFunnelResult, build_attribution_tool_registry

pytestmark = pytest.mark.security

_SECONDARY_ACTOR = Principal(
    subject_id="secondary-operator",
    tenant_id="tenant-secondary",
    kind="human",
    roles=("operator",),
    authn_method="trusted-test-profile",
)
_SECONDARY_EXECUTOR = Principal(
    subject_id="secondary-runtime",
    tenant_id="tenant-secondary",
    kind="service",
    roles=("runtime",),
    authn_method="trusted-test-profile",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _setup(tmp_path: Path):
    query_database = tmp_path / "analytics.db"
    label_database = tmp_path / "labels.db"
    generate_attribution_fixture(query_database, label_database)
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    runtime = await build_runtime(
        config,
        trusted_actors=(local_operator(), _SECONDARY_ACTOR),
        trusted_executors=(local_cli_executor(), _SECONDARY_EXECUTOR),
    )
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="v04-security-session",
        thread_id="v04-security-thread",
        run_id="v04-security-run",
    )
    registry = build_attribution_tool_registry(
        AnalyticsQueryStore(query_database), runtime.retriever
    )
    return runtime, ctx, registry, query_database, label_database


@pytest.mark.asyncio
async def test_same_query_is_scoped_by_the_trusted_context_tenant(tmp_path: Path) -> None:
    runtime, local_ctx, registry, _, _ = await _setup(tmp_path)
    secondary_ctx = runtime.new_context(
        actor=_SECONDARY_ACTOR,
        executor=_SECONDARY_EXECUTOR,
        session_id="secondary-session",
        thread_id="secondary-thread",
        run_id="secondary-run",
    )
    params = {
        "period": {"start_date": "2026-08-31", "end_date": "2026-08-31"},
        "dimensions": ["event_date"],
        "region": "east",
        "category": "full_service",
    }
    try:
        local = QueryFunnelResult.model_validate(
            (await registry.execute("query_funnel", params, local_ctx)).data
        )
        secondary = QueryFunnelResult.model_validate(
            (await registry.execute("query_funnel", params, secondary_ctx)).data
        )
        assert local.rows[0].metrics.redemption_rate < 0.37
        assert secondary.rows[0].metrics.redemption_rate > 0.65
        assert local.evidence.evidence_id != secondary.evidence.evidence_id
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sql_tools_are_parameterized_tenant_scoped_and_leave_database_unchanged(
    tmp_path: Path,
) -> None:
    runtime, ctx, registry, query_database, _ = await _setup(tmp_path)
    before = _sha256(query_database)
    try:
        result = QueryFunnelResult.model_validate(
            (
                await registry.execute(
                    "query_funnel",
                    {
                        "period": {"start_date": "2026-08-18", "end_date": "2026-09-01"},
                        "dimensions": ["region"],
                        "region": "east' OR tenant_id = 'tenant-secondary",
                    },
                    ctx,
                )
            ).data
        )
        assert result.rows == ()
        assert result.evidence.filters == {"region": "east' OR tenant_id = 'tenant-secondary"}
    finally:
        await runtime.aclose()
    assert _sha256(query_database) == before


@pytest.mark.asyncio
async def test_label_database_cannot_be_used_as_an_analytics_query_source(tmp_path: Path) -> None:
    runtime, ctx, _, _, label_database = await _setup(tmp_path)
    registry = build_attribution_tool_registry(
        AnalyticsQueryStore(label_database), runtime.retriever
    )
    try:
        with pytest.raises(AnalyticsQueryError, match="schema version"):
            await registry.execute(
                "query_funnel",
                {
                    "period": {"start_date": "2026-08-18", "end_date": "2026-09-01"},
                    "dimensions": ["event_date"],
                },
                ctx,
            )
    finally:
        await runtime.aclose()
