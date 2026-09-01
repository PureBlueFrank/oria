"""Installed runtime contracts for the two V0.1 read-only tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.errors import RuleSnapshotError
from oria.tools.models import QueryMerchantsResult, SearchCampaignRulesResult

pytestmark = pytest.mark.contract


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="t06-session",
        thread_id="t06-thread",
        run_id="t06-run",
    )
    await ctx.knowledge.ingest(demo_rule_document(), ctx)
    return runtime, ctx


@pytest.mark.asyncio
async def test_registered_tools_return_versioned_cited_and_bounded_results(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        assert tuple(ctx.tools) == (
            "search_campaign_rules",
            "query_merchants",
            "submit_assortment",
            "publish_consumer_placement",
            "send_merchant_notification",
        )
        assert ctx.tools.allowlist == frozenset(
            {
                "search_campaign_rules",
                "query_merchants",
                "submit_assortment",
                "publish_consumer_placement",
                "send_merchant_notification",
            }
        )
        assert {spec.schema_version for spec in ctx.tools.specs()} == {1}

        search = await ctx.tools.execute(
            "search_campaign_rules",
            {
                "intent": "merchant_recruitment",
                "effective_at": "2026-07-15T00:00:00+08:00",
            },
            ctx,
        )
        search_data = SearchCampaignRulesResult.model_validate(search.data)

        assert search.ok is True
        assert search.trust_level == "trusted_internal"
        assert search.provenance == "oria://tool/search_campaign_rules/v1"
        assert search_data.schema_version == 1
        assert search_data.rules is not None
        assert search_data.rule_snapshot_id is not None
        assert search_data.snapshot_hash is not None
        assert search_data.unresolved_items == ()
        assert {path.split(".", maxsplit=1)[0] for path in search_data.field_evidence} == {
            "basic",
            "recruitment_scope",
            "enrollment_policy",
            "benefit_policy",
            "confirmation_policy",
            "merchant_material",
        }
        assert all(
            [
                await ctx.knowledge.citation_exists(citation, ctx)
                for citation in search_data.field_evidence.values()
            ]
        )

        merchants = await ctx.tools.execute(
            "query_merchants",
            {"rule_snapshot_id": search_data.rule_snapshot_id, "limit": 10},
            ctx,
        )
        merchant_data = QueryMerchantsResult.model_validate(merchants.data)

        assert merchant_data.evaluated_count == 12
        assert merchant_data.eligible_count == merchant_data.returned_count == 10
        assert merchant_data.excluded_count == 2
        assert [item.merchant_id for item in merchant_data.candidates] == [
            "demo-m001",
            "demo-m002",
            "demo-m005",
            "demo-m006",
            "demo-m007",
            "demo-m008",
            "demo-m009",
            "demo-m010",
            "demo-m011",
            "demo-m012",
        ]
        assert merchant_data.exclusion_reason_counts == {
            "category_mismatch": 1,
            "city_mismatch": 1,
            "denylisted": 1,
            "enrollment_system_mismatch": 1,
            "inactive": 1,
            "not_allowlisted": 1,
            "sales_org_mismatch": 1,
        }

        limited = QueryMerchantsResult.model_validate(
            (
                await ctx.tools.execute(
                    "query_merchants",
                    {"rule_snapshot_id": search_data.rule_snapshot_id, "limit": 2},
                    ctx,
                )
            ).data
        )
        assert limited.eligible_count == 10
        assert limited.returned_count == len(limited.candidates) == 2
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_tool_params_reject_unknown_fields_enums_time_and_snapshots(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        valid_search = {
            "intent": "merchant_recruitment",
            "effective_at": "2026-07-15T00:00:00+08:00",
        }
        with pytest.raises(JsonSchemaValidationError):
            await ctx.tools.execute(
                "search_campaign_rules", {**valid_search, "tenant_id": "other"}, ctx
            )
        with pytest.raises(JsonSchemaValidationError):
            await ctx.tools.execute(
                "search_campaign_rules", {**valid_search, "intent": "invented"}, ctx
            )
        with pytest.raises(ValidationError, match="timezone"):
            await ctx.tools.execute(
                "search_campaign_rules",
                {**valid_search, "effective_at": "2026-07-15T00:00:00"},
                ctx,
            )
        with pytest.raises(JsonSchemaValidationError):
            await ctx.tools.execute(
                "query_merchants",
                {"rule_snapshot_id": "not-a-snapshot", "limit": 10},
                ctx,
            )
        with pytest.raises(RuleSnapshotError, match="unavailable"):
            await ctx.tools.execute(
                "query_merchants",
                {"rule_snapshot_id": "rs_" + "x" * 24, "limit": 10},
                ctx,
            )
    finally:
        await runtime.aclose()
