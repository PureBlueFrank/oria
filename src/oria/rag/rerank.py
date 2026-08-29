"""Deterministic reranker fixture for retrieval contract tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from oria.core.types import Doc
from oria.rag.bm25 import tokenize

if TYPE_CHECKING:
    from oria.core.context import Context


class FixtureReranker:
    """Rank by lexical query coverage with the incoming score as a stable tie-breaker.

    This fixture establishes the asynchronous reranker seam without loading a
    cross-encoder. Its output score is deterministic test data, not a relevance
    probability or a real-model quality measurement.
    """

    async def rerank(self, query: str, docs: list[Doc], ctx: Context) -> list[Doc]:
        del ctx
        query_terms = frozenset(tokenize(query))
        reranked: list[Doc] = []
        for doc in docs:
            document_terms = frozenset(tokenize(doc.content))
            coverage = (
                len(query_terms.intersection(document_terms)) / len(query_terms)
                if query_terms
                else 0.0
            )
            reranked.append(doc.model_copy(update={"score": coverage + doc.score * 0.001}))
        reranked.sort(key=lambda doc: (-doc.score, doc.id, doc.version))
        return reranked
