"""Configurable retrieval and reranker unit contracts for V0.2-T04."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from oria.core.types import ACLMetadata, Doc, QueryFilters
from oria.rag.pipeline import ConfigurableRetriever
from oria.rag.rerank import FixtureReranker

if TYPE_CHECKING:
    from oria.core.context import Context

pytestmark = pytest.mark.unit


def _doc(identifier: str, content: str, score: float) -> Doc:
    return Doc(
        id=identifier,
        version="1",
        tenant_id="tenant-a",
        content=content,
        metadata={"document_id": identifier},
        score=score,
        source_uri=f"fixture://{identifier}",
        acl=ACLMetadata(),
        trust_level="untrusted_data",
        provenance=f"fixture://{identifier}",
        data_classification="internal",
    )


class _FixtureRetriever:
    def __init__(self, docs: list[Doc]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int, QueryFilters | None]] = []

    async def retrieve(
        self,
        query: str,
        ctx: Context,
        k: int = 5,
        query_filters: QueryFilters | None = None,
    ) -> list[Doc]:
        del ctx
        self.calls.append((query, k, query_filters))
        return self.docs[:k]


@pytest.mark.asyncio
async def test_rrf_rewards_documents_recalled_by_both_projections() -> None:
    shared = _doc("shared", "shared result", 0.2)
    dense = _FixtureRetriever([_doc("dense", "dense result", 0.9), shared])
    bm25 = _FixtureRetriever([shared, _doc("lexical", "lexical result", 9.0)])
    pipeline = ConfigurableRetriever(mode="hybrid", dense=dense, bm25=bm25)

    docs = await pipeline.retrieve("result", object(), k=3)

    assert [doc.id for doc in docs] == ["shared", "dense", "lexical"]
    assert dense.calls[0][1] == bm25.calls[0][1] == 6


@pytest.mark.asyncio
async def test_fixture_reranker_prioritizes_lexical_coverage() -> None:
    dense = _FixtureRetriever(
        [
            _doc("semantic", "campaign overview", 0.9),
            _doc("literal", "coupon budget limit", 0.1),
        ]
    )
    bm25 = _FixtureRetriever([])
    pipeline = ConfigurableRetriever(
        mode="hybrid_rerank",
        dense=dense,
        bm25=bm25,
        reranker=FixtureReranker(),
    )

    docs = await pipeline.retrieve("coupon budget", object(), k=2)

    assert [doc.id for doc in docs] == ["literal", "semantic"]


def test_configured_components_are_required_without_fallback() -> None:
    dense = _FixtureRetriever([])
    with pytest.raises(ValueError, match="BM25"):
        ConfigurableRetriever(mode="hybrid", dense=dense)
    with pytest.raises(ValueError, match="reranker"):
        ConfigurableRetriever(mode="hybrid_rerank", dense=dense, bm25=dense)
