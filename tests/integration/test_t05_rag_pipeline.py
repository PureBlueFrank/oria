"""Real local SQLite/ObjectStore/Chroma integration contracts for V0.1-T05."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import CitationBlock, QueryFilters
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.embeddings import FixtureEmbedder
from oria.rag.demo import demo_rule_document
from oria.rag.errors import KnowledgeError, RuleSnapshotError
from oria.rag.index import ChromaIndex
from oria.rag.service import AuthorizedChromaRetriever, LocalKnowledgeService

pytestmark = pytest.mark.integration


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="rag-session",
        thread_id="rag-thread",
        run_id="rag-run",
    )
    return runtime, ctx


@pytest.mark.asyncio
async def test_ingest_retrieve_rebuild_and_delete_propagate_across_stores(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        request = demo_rule_document()
        first = await ctx.knowledge.ingest(request, ctx)
        second = await ctx.knowledge.ingest(request, ctx)
        assert first.document_id == second.document_id == "demo-campaign-rules"
        assert first.document_version == second.document_version == "1.0.0"
        assert first.chunk_count == second.chunk_count == 6
        assert second.idempotent is True

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
        for query, expected_category in questions:
            docs = await ctx.retriever.retrieve(query, ctx, k=3)
            if any(doc.metadata.get("rule_category") == expected_category for doc in docs):
                hits += 1
            for doc in docs:
                citation = CitationBlock(
                    document_id=str(doc.metadata["document_id"]),
                    document_version=doc.version,
                    chunk_id=doc.id,
                )
                assert await ctx.knowledge.citation_exists(citation, ctx) is True
        assert hits == len(questions)

        rebuilt = await ctx.knowledge.rebuild(ctx)
        assert rebuilt.document_versions == 1
        assert rebuilt.chunk_count == 6
        assert await ctx.retriever.retrieve("优惠档位", ctx, k=3)

        deleted = await ctx.knowledge.delete(request.document_id, ctx)
        assert deleted.deleted_versions == 1
        assert await ctx.retriever.retrieve("优惠档位", ctx, k=3) == []
        assert not config_escape_paths(tmp_path)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_rule_snapshot_has_leaf_evidence_and_round_trips_by_tenant_hash(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        docs = await ctx.retriever.retrieve(
            "campaign rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": "demo-campaign-rules"}),
        )
        resolution = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert resolution.unresolved_items == ()
        assert resolution.snapshot is not None
        snapshot = resolution.snapshot
        assert set(snapshot.categories()) == {
            "basic",
            "recruitment_scope",
            "enrollment_policy",
            "benefit_policy",
            "confirmation_policy",
            "merchant_material",
        }
        assert "basic.campaign_window" in snapshot.field_evidence
        assert "recruitment_scope.allowlist_merchant_ids.0" in snapshot.field_evidence
        assert "benefit_policy.tier_rules.0.fixed_amount" in snapshot.field_evidence
        assert "benefit_policy.tier_rules.1.steps.0.threshold" in snapshot.field_evidence
        assert "benefit_policy.tier_rules.1.steps.0.funding_amount" in snapshot.field_evidence
        citation_checks = [
            await ctx.knowledge.citation_exists(evidence.as_citation(), ctx)
            for evidence in snapshot.field_evidence.values()
        ]
        assert all(citation_checks)
        assert snapshot.recompute_hash() == snapshot.snapshot_hash

        repeated = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=snapshot.effective_at,
            ctx=ctx,
        )
        assert repeated.snapshot is not None
        assert repeated.snapshot.snapshot_id == snapshot.snapshot_id
        loaded = await ctx.rule_snapshots.get(snapshot.snapshot_id, ctx)
        assert loaded == snapshot
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_embedding_profile_switch_reuses_catalog_with_an_independent_collection(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        request = demo_rule_document()
        await ctx.knowledge.ingest(request, ctx)
        switched_embedder = FixtureEmbedder(dim=32)
        async with ChromaIndex(
            runtime.config.data_paths.chroma,
            projection_id="profile-bge-fixture",
            embedding_dimension=switched_embedder.dim,
        ) as switched_index:
            switched_knowledge = LocalKnowledgeService(
                catalog=ctx.knowledge._catalog,
                objects=ctx.knowledge._objects,
                index=switched_index,
                embedder=switched_embedder,
                embedding_profile="profile-bge-fixture",
            )
            switched_retriever = AuthorizedChromaRetriever(
                catalog=ctx.knowledge._catalog,
                index=switched_index,
                embedder=switched_embedder,
                knowledge=switched_knowledge,
            )

            repeated = await switched_knowledge.ingest(request, ctx)
            rebuilt = await switched_knowledge.rebuild(ctx)
            docs = await switched_retriever.retrieve("优惠档位", ctx, k=10)

        assert repeated.idempotent is True
        assert rebuilt.chunk_count == 6
        assert len(docs) == 6
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_rule_resolution_reports_missing_conflicting_and_invalid_sections(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        original = demo_rule_document()
        await ctx.knowledge.ingest(original, ctx)
        original_docs = await ctx.retriever.retrieve(
            "rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": original.document_id}),
        )
        missing = await ctx.rule_snapshots.resolve(
            [doc for doc in original_docs if doc.metadata.get("rule_category") != "benefit_policy"],
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert missing.snapshot is None
        assert missing.unresolved_items == ("missing:benefit_policy",)

        conflicting_payload = json.loads(original.content)
        conflicting_payload["basic"]["campaign_type"] = "conflicting_type"
        conflicting = original.model_copy(
            update={
                "document_id": "conflicting-campaign-rules",
                "content": json.dumps(conflicting_payload, ensure_ascii=False),
            }
        )
        await ctx.knowledge.ingest(conflicting, ctx)
        conflicting_docs = await ctx.retriever.retrieve(
            "rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": conflicting.document_id}),
        )
        conflict = await ctx.rule_snapshots.resolve(
            original_docs + conflicting_docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert conflict.snapshot is None
        assert conflict.unresolved_items == ("conflict:basic",)

        invalid_payload = json.loads(original.content)
        invalid_payload["benefit_policy"]["tier_rules"][0] = {
            "name": "base",
            "funding_type": "discount_rate",
            "discount_rate": "1.1",
        }
        invalid = original.model_copy(
            update={
                "document_id": "invalid-campaign-rules",
                "content": json.dumps(invalid_payload, ensure_ascii=False),
                "metadata": {**original.metadata, "priority": 200},
            }
        )
        await ctx.knowledge.ingest(invalid, ctx)
        invalid_docs = await ctx.retriever.retrieve(
            "rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": invalid.document_id}),
        )
        invalid_resolution = await ctx.rule_snapshots.resolve(
            invalid_docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert invalid_resolution.snapshot is None
        assert invalid_resolution.unresolved_items == ("invalid_rule:ValidationError",)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_deleted_sources_invalidate_snapshot_and_tampered_objects_block_rebuild(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        request = demo_rule_document()
        ingested = await ctx.knowledge.ingest(request, ctx)
        docs = await ctx.retriever.retrieve("rules", ctx, k=10)
        resolution = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert resolution.snapshot is not None
        snapshot_id = resolution.snapshot.snapshot_id

        object_path = runtime.config.data_paths.objects.joinpath(
            *ingested.object_ref.removeprefix("object://").split("/")
        )
        object_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="integrity"):
            await ctx.knowledge.rebuild(ctx)

        object_path.write_text(request.content, encoding="utf-8")
        await ctx.knowledge.rebuild(ctx)
        await ctx.knowledge.delete(request.document_id, ctx)
        with pytest.raises(RuleSnapshotError, match="stale"):
            await ctx.rule_snapshots.get(snapshot_id, ctx)
    finally:
        await runtime.aclose()


def config_escape_paths(tmp_path: Path) -> list[Path]:
    allowed = tmp_path / "data"
    return [path for path in tmp_path.rglob("*") if not path.is_relative_to(allowed)]
