"""Allowlist, authorization, and sensitive-result boundaries for V0.1 tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import PolicyDecision
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document

pytestmark = pytest.mark.security


class _DenyPolicy:
    async def authorize(self, request: object, ctx: object) -> PolicyDecision:
        del request, ctx
        return PolicyDecision(
            allow=False,
            policy_version="deny-test-v1",
            reason="test denial",
        )


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="security-tool-session",
        thread_id="security-tool-thread",
        run_id="security-tool-run",
    )
    await ctx.knowledge.ingest(demo_rule_document(), ctx)
    return runtime, ctx


@pytest.mark.asyncio
async def test_model_visible_tool_schemas_and_results_exclude_restricted_members(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        search_tool = ctx.tools.get("search_campaign_rules")
        query_tool = ctx.tools.get("query_merchants")
        schemas = json.dumps(
            [search_tool.result_schema, query_tool.result_schema],
            ensure_ascii=False,
            sort_keys=True,
        )
        for restricted_name in (
            "allowlist_merchant_ids",
            "denylist_merchant_ids",
            "sales_org_scope",
            "sales_org_code",
        ):
            assert restricted_name not in schemas

        search = await ctx.tools.execute(
            "search_campaign_rules",
            {
                "intent": "merchant_recruitment",
                "effective_at": "2026-07-15T00:00:00+08:00",
            },
            ctx,
        )
        snapshot_id = search.data["rule_snapshot_id"]
        query = await ctx.tools.execute(
            "query_merchants",
            {"rule_snapshot_id": snapshot_id, "limit": 100},
            ctx,
        )
        visible = json.dumps([search.data, query.data], ensure_ascii=False, sort_keys=True)
        for restricted_name in (
            "allowlist_merchant_ids",
            "denylist_merchant_ids",
            "sales_org_scope",
            "sales_org_code",
        ):
            assert restricted_name not in visible
        for restricted_value in (
            "demo-m004",
            "synthetic-east-a",
            "synthetic-east-b",
        ):
            assert restricted_value not in visible
        assert query.trust_level == "trusted_internal"
        assert query.provenance == "oria://tool/query_merchants/v1"
        assert query.data_classification == "internal"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_registry_rejects_non_allowlisted_and_policy_denied_execution(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        with pytest.raises(LookupError, match="allowlisted"):
            await ctx.tools.execute("persist_campaign", {}, ctx)

        object.__setattr__(runtime, "policy", _DenyPolicy())
        with pytest.raises(PermissionError, match="not authorized"):
            await ctx.tools.execute(
                "search_campaign_rules",
                {
                    "intent": "merchant_recruitment",
                    "effective_at": "2026-07-15T00:00:00+08:00",
                },
                ctx,
            )
    finally:
        await runtime.aclose()
