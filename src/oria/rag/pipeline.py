"""Configurable dense, hybrid, and reranked retrieval pipelines."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from oria.core.protocols import Reranker, Retriever
from oria.core.types import Doc, QueryFilters

if TYPE_CHECKING:
    from oria.core.context import Context

RetrievalMode = Literal["dense", "hybrid", "hybrid_rerank"]


class ConfigurableRetriever:
    """Expose three retrieval strategies through the core Retriever contract.

    Hybrid mode uses Reciprocal Rank Fusion because dense similarity and BM25
    scores have unrelated scales. RRF depends only on rank and rewards documents
    found by both projections. Missing configured components are rejected during
    construction; a selected strategy is never silently downgraded.
    """

    def __init__(
        self,
        *,
        mode: RetrievalMode,
        dense: Retriever,
        bm25: Retriever | None = None,
        reranker: Reranker | None = None,
        rrf_rank_constant: int = 60,
    ) -> None:
        if mode not in {"dense", "hybrid", "hybrid_rerank"}:
            raise ValueError("unsupported retrieval mode")
        if rrf_rank_constant <= 0:
            raise ValueError("RRF rank constant must be positive")
        if mode in {"hybrid", "hybrid_rerank"} and bm25 is None:
            raise ValueError("hybrid retrieval requires a BM25 retriever")
        if mode == "hybrid_rerank" and reranker is None:
            raise ValueError("reranked retrieval requires a reranker")
        if mode != "hybrid_rerank" and reranker is not None:
            raise ValueError("reranker is only valid for hybrid_rerank mode")
        self._mode = mode
        self._dense = dense
        self._bm25 = bm25
        self._reranker = reranker
        self._rrf_rank_constant = rrf_rank_constant

    def for_mode(
        self,
        mode: RetrievalMode,
        *,
        reranker: Reranker | None = None,
    ) -> ConfigurableRetriever:
        """Create another configured view over the same authorized projections."""

        return ConfigurableRetriever(
            mode=mode,
            dense=self._dense,
            bm25=self._bm25,
            reranker=reranker,
            rrf_rank_constant=self._rrf_rank_constant,
        )

    async def retrieve(
        self,
        query: str,
        ctx: Context,
        k: int = 5,
        query_filters: QueryFilters | None = None,
    ) -> list[Doc]:
        if not query.strip():
            raise ValueError("retrieval query must be non-empty")
        if not 1 <= k <= 50:
            raise ValueError("k must be between 1 and 50")
        if self._mode == "dense":
            return await self._dense.retrieve(query, ctx, k, query_filters)

        candidate_count = min(k * 2, 50)
        if self._bm25 is None:
            raise RuntimeError("configured BM25 retriever became unavailable")
        dense_docs, bm25_docs = await asyncio.gather(
            self._dense.retrieve(query, ctx, candidate_count, query_filters),
            self._bm25.retrieve(query, ctx, candidate_count, query_filters),
        )
        fused = self._reciprocal_rank_fusion(dense_docs, bm25_docs)
        if self._mode == "hybrid_rerank":
            if self._reranker is None:
                raise RuntimeError("configured reranker became unavailable")
            fused = await self._reranker.rerank(query, fused, ctx)
        return fused[:k]

    def _reciprocal_rank_fusion(
        self,
        dense_docs: list[Doc],
        bm25_docs: list[Doc],
    ) -> list[Doc]:
        documents: dict[tuple[str, str, str], Doc] = {}
        scores: dict[tuple[str, str, str], float] = {}
        for ranking in (dense_docs, bm25_docs):
            for rank, doc in enumerate(ranking, start=1):
                identity = (doc.tenant_id, doc.id, doc.version)
                documents.setdefault(identity, doc)
                scores[identity] = scores.get(identity, 0.0) + 1.0 / (
                    self._rrf_rank_constant + rank
                )
        fused = [
            doc.model_copy(update={"score": scores[identity]})
            for identity, doc in documents.items()
        ]
        fused.sort(key=lambda doc: (-doc.score, doc.id, doc.version))
        return fused
