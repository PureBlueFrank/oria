"""Verify T05 ingest, retrieval, citation, and snapshot behavior from an installed wheel."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

import oria
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.errors import RuleSnapshotError


async def _verify(data_dir: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=data_dir)
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="t05-wheel-session",
        thread_id="t05-wheel-thread",
        run_id="t05-wheel-run",
    )
    try:
        request = demo_rule_document()
        ingested = await ctx.knowledge.ingest(request, ctx)
        questions = (
            ("活动模板和报名时间", "basic"),
            ("活动商品范围", "basic"),
            ("餐饮商家城市和报名系统", "recruitment_scope"),
            ("招商黑白名单判定", "recruitment_scope"),
            ("商品圈选价格类目关键词", "enrollment_policy"),
            ("招后选品策略和完成条件", "enrollment_policy"),
            ("基础档固定金额和预算上限", "benefit_policy"),
            ("膨胀档阶梯出资", "benefit_policy"),
            ("商家销售销售经理确认顺序", "confirmation_policy"),
            ("活动标题头图介绍标签", "merchant_material"),
        )
        hits = 0
        for query, category in questions:
            docs = await ctx.retriever.retrieve(query, ctx, k=3)
            hits += any(doc.metadata.get("rule_category") == category for doc in docs)
        if hits != len(questions):
            raise AssertionError(f"installed T05 fixture Recall@3 was {hits}/{len(questions)}")

        docs = await ctx.retriever.retrieve("campaign rules", ctx, k=10)
        visible = "\n".join(doc.content for doc in docs)
        if "demo-m004" in visible or "synthetic-east-a" in visible:
            raise AssertionError("installed T05 retriever disclosed restricted rule members")
        resolution = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        snapshot = resolution.snapshot
        if snapshot is None or resolution.unresolved_items:
            raise AssertionError("installed T05 snapshot did not resolve")
        required_evidence = {
            "benefit_policy.tier_rules.0.fixed_amount",
            "benefit_policy.tier_rules.1.steps.0.threshold",
            "enrollment_policy.assortment_policy_version",
        }
        if not required_evidence.issubset(snapshot.field_evidence):
            raise AssertionError("installed T05 snapshot is missing leaf evidence")
        if snapshot.recompute_hash() != snapshot.snapshot_hash:
            raise AssertionError("installed T05 snapshot hash is not reproducible")

        rebuilt = await ctx.knowledge.rebuild(ctx)
        if rebuilt.chunk_count != ingested.chunk_count:
            raise AssertionError("installed T05 projection rebuild changed the chunk count")
        await ctx.knowledge.delete(request.document_id, ctx)
        try:
            await ctx.rule_snapshots.get(snapshot.snapshot_id, ctx)
        except RuleSnapshotError:
            pass
        else:
            raise AssertionError("installed T05 stale snapshot was accepted")
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
    print(f"verified installed T05 RAG and snapshot behavior from {package_file}")


if __name__ == "__main__":
    main()
