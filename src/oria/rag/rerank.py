"""Deterministic reranker fixture for retrieval contract tests."""

from __future__ import annotations

import asyncio
import importlib
import math
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, cast

from oria.core.types import Doc
from oria.rag.bm25 import tokenize

if TYPE_CHECKING:
    from oria.core.context import Context


class _CrossEncoder(Protocol):
    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> object: ...


CrossEncoderFactory = Callable[..., _CrossEncoder]


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


class CrossEncoderReranker:
    """Pinned local cross-encoder with remote model code disabled."""

    def __init__(
        self,
        *,
        model: str,
        revision: str | None,
        trust_remote_code: bool,
        model_factory: CrossEncoderFactory | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("cross-encoder model must be non-empty")
        if not revision:
            raise ValueError("cross-encoder revision must be pinned")
        if trust_remote_code:
            raise ValueError("cross-encoder remote model code is forbidden")
        factory = model_factory if model_factory is not None else self._load_factory()
        self._model = factory(model, revision=revision, trust_remote_code=False)

    @staticmethod
    def _load_factory() -> CrossEncoderFactory:
        try:
            module = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for the selected reranker"
            ) from exc
        return cast(CrossEncoderFactory, module.CrossEncoder)

    async def rerank(self, query: str, docs: list[Doc], ctx: Context) -> list[Doc]:
        del ctx
        if not query.strip():
            raise ValueError("reranker query must be non-empty")
        if not docs:
            return []
        predicted = await asyncio.to_thread(
            self._model.predict,
            [(query, doc.content) for doc in docs],
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        raw = predicted.tolist() if hasattr(predicted, "tolist") else predicted
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError("cross-encoder returned an invalid score batch")
        scores = [float(value) for value in raw]
        if len(scores) != len(docs) or any(not math.isfinite(score) for score in scores):
            raise ValueError("cross-encoder returned an invalid score batch")
        reranked = [
            doc.model_copy(update={"score": score}) for doc, score in zip(docs, scores, strict=True)
        ]
        reranked.sort(key=lambda doc: (-doc.score, doc.id, doc.version))
        return reranked
