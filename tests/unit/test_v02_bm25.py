"""Pure-Python BM25 scoring and filter contracts for V0.2-T04."""

from __future__ import annotations

import pytest

from oria.core.types import ACLFilter, ACLMetadata
from oria.rag.bm25 import BM25Index
from oria.rag.errors import IndexError
from oria.rag.models import CatalogVersion, IndexedChunk

pytestmark = pytest.mark.unit


def _catalog(*, tenant_id: str = "tenant-a", acl: ACLMetadata | None = None) -> CatalogVersion:
    return CatalogVersion(
        tenant_id=tenant_id,
        document_id="doc-a",
        version="1",
        source_uri="fixture://doc-a",
        owner_ref="fixture-owner",
        data_classification="internal",
        content_hash=f"sha256:{'1' * 64}",
        object_ref="fixture-object",
        acl=acl or ACLMetadata(),
        metadata={},
        chunking_version="fixture-v1",
        embedding_profile="fixture",
    )


def _chunk(chunk_id: str, content: str) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        document_id="doc-a",
        document_version="1",
        content_hash=f"sha256:{'1' * 64}",
        public_content=content,
    )


def _acl(*, tenant_id: str = "tenant-a", subject_ids: tuple[str, ...] = ()) -> ACLFilter:
    return ACLFilter(
        tenant_id=tenant_id,
        allowed_subject_ids=subject_ids,
        classifications=("internal",),
    )


@pytest.mark.asyncio
async def test_bm25_saturates_tf_and_normalizes_document_length() -> None:
    index = BM25Index()
    short = _chunk(f"chk_{'1' * 32}", "coupon budget")
    long = _chunk(f"chk_{'2' * 32}", "coupon " + "unrelated " * 30)
    await index.upsert(_catalog(), (short, long))

    hits = await index.query("coupon", acl_filter=_acl(), k=2, filters={})

    assert [hit.chunk_id for hit in hits] == [short.chunk_id, long.chunk_id]
    assert hits[0].score > hits[1].score > 0


@pytest.mark.asyncio
async def test_bm25_requires_tenant_build_and_applies_acl_before_scoring() -> None:
    index = BM25Index()
    with pytest.raises(IndexError, match="not built"):
        await index.query("secret", acl_filter=_acl(), k=5, filters={})

    denied = _chunk(f"chk_{'3' * 32}", "secret coupon")
    await index.upsert(_catalog(acl=ACLMetadata(allowed_subject_ids=("allowed",))), (denied,))

    assert (
        await index.query("secret", acl_filter=_acl(subject_ids=("other",)), k=5, filters={}) == ()
    )
    allowed = await index.query(
        "secret", acl_filter=_acl(subject_ids=("allowed",)), k=5, filters={}
    )
    assert [hit.chunk_id for hit in allowed] == [denied.chunk_id]
