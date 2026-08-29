"""V0.2-T03 versioned knowledge lifecycle integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ACLMetadata
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.errors import CatalogError

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
