"""V0.2-T03 versioned knowledge lifecycle integration tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ACLMetadata, CitationBlock, Doc
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.embeddings import FixtureEmbedder
from oria.rag.demo import demo_rule_document
from oria.rag.errors import CatalogError, IndexError, KnowledgeError, ObjectStoreError
from oria.rag.index import ChromaIndex
from oria.rag.service import LocalKnowledgeService

pytestmark = pytest.mark.integration


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="v02-rag-session",
        thread_id="v02-rag-thread",
        run_id="v02-rag-run",
    )
    return runtime, ctx


def _updated_document():
    original = demo_rule_document()
    payload = json.loads(original.content)
    payload["basic"]["campaign_type"] = "updated_campaign"
    return original.model_copy(
        update={
            "version": "2.0.0",
            "owner_ref": "oria-synthetic-owner-v2",
            "data_classification": "internal",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "acl": ACLMetadata(allowed_subject_ids=("local-operator",)),
            "metadata": {**original.metadata, "priority": 200, "supersedes": "1.0.0"},
        }
    )


def _citation(doc: Doc) -> CitationBlock:
    return CitationBlock(
        document_id=str(doc.metadata["document_id"]),
        document_version=doc.version,
        chunk_id=doc.id,
    )


@pytest.mark.asyncio
async def test_new_version_versions_owner_acl_classification_and_supersedes_old(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        original = demo_rule_document()
        updated = _updated_document()
        await ctx.knowledge.ingest(original, ctx)
        await ctx.knowledge.ingest(updated, ctx)

        active = await ctx.knowledge._catalog.list_active_versions(ctx.tenant_id)
        history = await ctx.knowledge._catalog.list_document_versions(
            ctx.tenant_id, original.document_id
        )

        assert [item.version for item in active] == ["2.0.0"]
        assert [item.version for item in history] == ["1.0.0", "2.0.0"]
        assert active[0].owner_ref == updated.owner_ref
        assert active[0].acl == updated.acl
        assert active[0].data_classification == "internal"
        assert (
            await ctx.knowledge._catalog.get_active_version(
                ctx.tenant_id, original.document_id, original.version
            )
            is None
        )

        with pytest.raises(CatalogError, match="immutable"):
            await ctx.knowledge.ingest(
                updated.model_copy(update={"owner_ref": "mutated-owner"}),
                ctx,
            )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_update_cleans_old_chunks_across_projections_and_rebuilds_only_active(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        original = demo_rule_document()
        await ctx.knowledge.ingest(original, ctx)
        old_docs = await ctx.retriever.retrieve("rules", ctx, k=10)
        assert len(old_docs) == 6

        second_embedder = FixtureEmbedder(dim=32)
        async with ChromaIndex(
            runtime.config.data_paths.chroma,
            projection_id="v02-secondary-projection",
            embedding_dimension=second_embedder.dim,
        ) as second_index:
            second_knowledge = LocalKnowledgeService(
                catalog=ctx.knowledge._catalog,
                objects=ctx.knowledge._objects,
                index=second_index,
                embedder=second_embedder,
                embedding_profile="v02-secondary-projection",
            )
            assert (await second_knowledge.rebuild(ctx)).chunk_count == 6

            await ctx.knowledge.ingest(_updated_document(), ctx)

            for doc in old_docs:
                assert not await ctx.knowledge._index.contains(doc.id, ctx.tenant_id)
                assert not await second_index.contains(doc.id, ctx.tenant_id)

        current_docs = await ctx.retriever.retrieve("rules", ctx, k=10)
        assert len(current_docs) == 6
        assert {doc.version for doc in current_docs} == {"2.0.0"}
        rebuilt = await ctx.knowledge.rebuild(ctx)
        assert rebuilt.document_versions == 1
        assert rebuilt.chunk_count == 6
        stored = await asyncio.to_thread(
            ctx.knowledge._index._collection.get,
            where={"tenant_id": {"$eq": ctx.tenant_id}},
            include=["metadatas"],
        )
        assert len(stored["ids"]) == rebuilt.chunk_count

        history = await ctx.knowledge._catalog.list_document_versions(
            ctx.tenant_id, original.document_id
        )
        current_citations = [_citation(doc) for doc in current_docs]
        assert all(
            [await ctx.knowledge.citation_exists(citation, ctx) for citation in current_citations]
        )
        deleted = await ctx.knowledge.delete(original.document_id, ctx)
        assert deleted.deleted_versions == 1
        after_delete = await asyncio.to_thread(
            ctx.knowledge._index._collection.get,
            where={"tenant_id": {"$eq": ctx.tenant_id}},
            include=["metadatas"],
        )
        assert after_delete["ids"] == []
        for version in history:
            with pytest.raises(ObjectStoreError, match="unavailable"):
                ctx.knowledge._objects.read_bytes(version.object_ref, ctx)
        for citation in current_citations:
            assert await ctx.knowledge.citation_exists(citation, ctx) is False
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_update_cleanup_failure_is_retryable_after_catalog_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        old_doc = (await ctx.retriever.retrieve("rules", ctx, k=1))[0]
        old_citation = _citation(old_doc)
        updated = _updated_document()
        original_delete = ctx.knowledge._index.delete_document_version_all_projections

        async def fail_delete(_tenant_id: str, _document_id: str, _document_version: str) -> None:
            raise IndexError("injected version cleanup failure")

        monkeypatch.setattr(
            ctx.knowledge._index,
            "delete_document_version_all_projections",
            fail_delete,
        )
        with pytest.raises(IndexError, match="injected version cleanup failure"):
            await ctx.knowledge.ingest(updated, ctx)

        docs = await ctx.retriever.retrieve("rules", ctx, k=10)
        assert {doc.version for doc in docs} == {"2.0.0"}
        assert await ctx.knowledge._index.contains(old_doc.id, ctx.tenant_id)
        assert await ctx.knowledge.citation_exists(old_citation, ctx) is False
        with pytest.raises(KnowledgeError, match="unavailable"):
            await ctx.knowledge.load_public_chunk(old_citation, ctx)

        monkeypatch.setattr(
            ctx.knowledge._index,
            "delete_document_version_all_projections",
            original_delete,
        )
        retried = await ctx.knowledge.ingest(updated, ctx)
        assert retried.idempotent is True
        assert not await ctx.knowledge._index.contains(old_doc.id, ctx.tenant_id)
    finally:
        await runtime.aclose()
