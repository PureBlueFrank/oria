"""Verify T06 allowlisted tools and redacted results from an installed wheel."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import oria
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.tools.models import QueryMerchantsResult, SearchCampaignRulesResult


async def _verify(data_dir: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=data_dir)
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="t06-wheel-session",
        thread_id="t06-wheel-thread",
        run_id="t06-wheel-run",
    )
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        if tuple(ctx.tools) != ("search_campaign_rules", "query_merchants"):
            raise AssertionError("installed T06 tool allowlist is invalid")
        search = SearchCampaignRulesResult.model_validate(
            (
                await ctx.tools.execute(
                    "search_campaign_rules",
                    {
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                    ctx,
                )
            ).data
        )
        if search.rule_snapshot_id is None or not search.field_evidence:
            raise AssertionError("installed T06 rule tool did not return a cited snapshot")
        merchants = QueryMerchantsResult.model_validate(
            (
                await ctx.tools.execute(
                    "query_merchants",
                    {"rule_snapshot_id": search.rule_snapshot_id, "limit": 10},
                    ctx,
                )
            ).data
        )
        if merchants.returned_count != 5 or merchants.excluded_count != 7:
            raise AssertionError(
                "installed T06 merchant tool returned unexpected eligibility counts"
            )
        visible = json.dumps(
            [search.model_dump(mode="json"), merchants.model_dump(mode="json")],
            ensure_ascii=False,
        )
        for restricted in ("demo-m004", "demo-m011", "synthetic-east-a"):
            if restricted in visible:
                raise AssertionError("installed T06 tools disclosed restricted eligibility inputs")
    finally:
        await runtime.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    data_dir = args.data_dir.resolve(strict=False)
    if data_dir.exists():
        raise AssertionError("wheel verification requires a fresh data directory")
    package_file = Path(oria.__file__).resolve()
    if "site-packages" not in package_file.parts:
        raise AssertionError("Oria was not imported from an installed wheel environment")
    asyncio.run(_verify(data_dir))
    print(f"verified installed T06 tools and redaction from {package_file}")


if __name__ == "__main__":
    main()
