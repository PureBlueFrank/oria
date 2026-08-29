"""Tenant, ACL, content-disclosure, and snapshot-tamper boundaries for T05."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ACLFilter, ACLMetadata, PolicyDecision, Principal, QueryFilters
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.errors import RuleSnapshotError

pytestmark = pytest.mark.security


class _TenantTestPolicy:
    async def authorize(self, request: object, ctx: object) -> PolicyDecision:
        del request
        tenant_id = ctx.tenant_id
        return PolicyDecision(
            allow=True,
            constraints={"tenant_id": tenant_id},
            policy_version="tenant-test-v1",
            reason="test tenant isolation",
            acl_filter=ACLFilter(
                tenant_id=tenant_id,
                allowed_subject_ids=(ctx.actor.subject_id,),
                allowed_roles=ctx.actor.roles,
                classifications=("public", "internal", "restricted"),
            ),
        )


async def _runtime(tmp_path: Path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="security-rag-session",
        thread_id="security-rag-thread",
        run_id="security-rag-run",
    )
    return runtime, ctx


@pytest.mark.asyncio
async def test_retriever_rejects_caller_acl_filters_and_postfilters_denied_docs(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        restricted = demo_rule_document().model_copy(
            update={
                "document_id": "restricted-rules",
                "acl": ACLMetadata(allowed_subject_ids=("someone-else",)),
            }
        )
        await ctx.knowledge.ingest(restricted, ctx)
        with pytest.raises(ValueError, match="reserved"):
            await ctx.retriever.retrieve(
                "rules", ctx, query_filters=QueryFilters(attributes={"tenant_id": "evil"})
            )
        docs = await ctx.retriever.retrieve(
            "rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": "restricted-rules"}),
        )
        assert docs == []
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_identical_documents_do_not_overwrite_another_tenant_projection(
    tmp_path: Path,
) -> None:
    runtime, first_ctx = await _runtime(tmp_path)
    try:
        object.__setattr__(runtime, "policy", _TenantTestPolicy())
        await first_ctx.knowledge.ingest(demo_rule_document(), first_ctx)
        second_actor = Principal(
            subject_id="second-operator",
            tenant_id="second-tenant",
            kind="human",
            roles=("operator",),
            authn_method="test",
        )
        second_executor = Principal(
            subject_id="second-runtime",
            tenant_id="second-tenant",
            kind="service",
            roles=("runtime",),
            authn_method="test",
        )
        second_ctx = runtime.new_context(
            actor=second_actor,
            executor=second_executor,
            session_id="second-session",
            thread_id="second-thread",
            run_id="second-run",
        )
        await second_ctx.knowledge.ingest(demo_rule_document(), second_ctx)

        first_docs = await first_ctx.retriever.retrieve("rules", first_ctx, k=10)
        second_docs = await second_ctx.retriever.retrieve("rules", second_ctx, k=10)
        assert len(first_docs) == len(second_docs) == 6
        assert {doc.id for doc in first_docs}.isdisjoint(doc.id for doc in second_docs)
        assert {doc.tenant_id for doc in first_docs} == {first_ctx.tenant_id}
        assert {doc.tenant_id for doc in second_docs} == {second_ctx.tenant_id}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_retriever_returns_object_truth_when_vector_content_is_tampered(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        before = await ctx.retriever.retrieve("优惠档位", ctx, k=10)
        target = next(
            doc for doc in before if doc.metadata.get("rule_category") == "benefit_policy"
        )

        index = ctx.knowledge._index
        stored = await asyncio.to_thread(
            index._collection.get,
            ids=[target.id],
            include=["embeddings"],
        )
        await asyncio.to_thread(
            index._collection.update,
            ids=[target.id],
            embeddings=stored["embeddings"],
            documents=["POISONED_VECTOR_CONTENT"],
        )

        after = await ctx.retriever.retrieve("优惠档位", ctx, k=10)
        observed = next(doc for doc in after if doc.id == target.id)
        assert observed.content == target.content
        assert "POISONED_VECTOR_CONTENT" not in observed.content
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_restricted_rule_members_never_enter_retrieved_content_or_public_snapshot(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        docs = await ctx.retriever.retrieve("黑白名单销售组织", ctx, k=10)
        visible = "\n".join(doc.content for doc in docs)
        assert "demo-m004" not in visible
        assert "synthetic-east-a" not in visible

        resolution = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert resolution.snapshot is not None
        snapshot = resolution.snapshot
        assert "demo-m004" in snapshot.recruitment_scope.internal_denylist()
        assert "demo-m004" not in snapshot.model_dump_json()
        assert "synthetic-east-a" not in snapshot.model_dump_json()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_snapshot_id_is_tenant_qualified_and_tampered_payload_fails_closed(
    tmp_path: Path,
) -> None:
    runtime, ctx = await _runtime(tmp_path)
    try:
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        docs = await ctx.retriever.retrieve("rules", ctx, k=10)
        resolution = await ctx.rule_snapshots.resolve(
            docs,
            effective_at=datetime.fromisoformat("2026-07-15T00:00:00+08:00"),
            ctx=ctx,
        )
        assert resolution.snapshot is not None
        snapshot_id = resolution.snapshot.snapshot_id

        forged_actor = Principal(
            subject_id="attacker",
            tenant_id="other-tenant",
            kind="human",
            roles=("operator",),
            authn_method="test",
        )
        forged_executor = Principal(
            subject_id="attacker-runtime",
            tenant_id="other-tenant",
            kind="service",
            roles=("runtime",),
            authn_method="test",
        )
        forged_ctx = runtime.new_context(
            actor=forged_actor,
            executor=forged_executor,
            session_id="forged-session",
            thread_id="forged-thread",
            run_id="forged-run",
        )
        with pytest.raises((PermissionError, RuleSnapshotError)):
            await ctx.rule_snapshots.get(snapshot_id, forged_ctx)

        with sqlite3.connect(runtime.config.data_paths.platform_db) as connection:
            connection.execute(
                "UPDATE rule_snapshot_cache SET payload_json = ? "
                "WHERE tenant_id = ? AND snapshot_id = ?",
                ('{"tampered":true}', ctx.tenant_id, snapshot_id),
            )
            connection.commit()
        with pytest.raises(RuleSnapshotError, match="integrity"):
            await ctx.rule_snapshots.get(snapshot_id, ctx)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_object_store_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    runtime, ctx = await _runtime(tmp_path)
    source = tmp_path / "data" / "reports-tmp" / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        with pytest.raises(ValueError):
            await ctx.objects.put("../escape", str(source), ctx)
        link = tmp_path / "data" / "objects" / ctx.tenant_id
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError):
            await ctx.objects.put(f"{ctx.tenant_id}/safe", str(source), ctx)
        assert list(outside.iterdir()) == []
    finally:
        await runtime.aclose()
