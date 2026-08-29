"""V0.2-T04 retrieval pipeline integration and raw latency baseline."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.pipeline import ConfigurableRetriever
from oria.rag.rerank import FixtureReranker

pytestmark = pytest.mark.integration


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="v02-t04-session",
        thread_id="v02-t04-thread",
        run_id="v02-t04-run",
    )
    return runtime, ctx


@pytest.mark.asyncio
async def test_three_pipelines_run_on_shared_data_with_raw_latency(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)

        dense = ctx.retriever._dense
        bm25 = ctx.retriever._bm25
        pipelines = {
            "dense": ConfigurableRetriever(mode="dense", dense=dense, bm25=bm25),
            "hybrid": ConfigurableRetriever(mode="hybrid", dense=dense, bm25=bm25),
            "hybrid_rerank": ConfigurableRetriever(
                mode="hybrid_rerank",
                dense=dense,
                bm25=bm25,
                reranker=FixtureReranker(),
            ),
        }

        results: dict[str, tuple[list[str], float]] = {}
        for name, pipeline in pipelines.items():
            started = time.perf_counter()
            docs = await pipeline.retrieve("华东餐饮暑期活动规则", ctx, k=10)
            elapsed = time.perf_counter() - started
            assert docs, f"{name} pipeline returned no documents"
            assert all(doc.trust_level == "untrusted_data" for doc in docs)
            results[name] = ([doc.id for doc in docs], elapsed)

        # Raw latency baseline only; no quality threshold is asserted here
        # (real Recall@K/MRR comparison is V0.2-T05 with a frozen holdout).
        assert set(results) == {"dense", "hybrid", "hybrid_rerank"}
        for name, (ids, _elapsed) in results.items():
            assert ids, f"{name} returned an empty id list"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_hybrid_fails_explicitly_when_bm25_is_not_built(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        dense = ctx.retriever._dense
        bm25 = ctx.retriever._bm25
        hybrid = ConfigurableRetriever(mode="hybrid", dense=dense, bm25=bm25)
        with pytest.raises(Exception, match="not built"):
            await hybrid.retrieve("rules", ctx, k=5)
    finally:
        await runtime.aclose()
