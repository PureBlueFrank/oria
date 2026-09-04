"""Pure-Python BM25 projection with policy-owned pre-filtering."""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from dataclasses import dataclass

from oria.core.types import ACLFilter, ACLMetadata, JsonValue
from oria.rag.errors import IndexError
from oria.rag.index import projection_metadata
from oria.rag.models import BM25Hit, CatalogVersion, IndexedChunk

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")


def tokenize(text: str) -> tuple[str, ...]:
    """Tokenize Latin terms and individual CJK characters without optional dependencies."""
    return tuple(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text))


@dataclass(frozen=True, slots=True)
class _BM25Document:
    chunk_id: str
    content: str
    tokens: tuple[str, ...]
    metadata: dict[str, JsonValue]
    acl: ACLMetadata


class BM25Index:
    """Rebuildable in-process lexical projection synchronized by KnowledgeService.

    The Robertson/Sparck Jones IDF is kept positive with
    ``log(1 + (N - df + 0.5) / (df + 0.5))``. This avoids negative scores for
    common terms while retaining the standard rarity signal. Term frequency is
    saturated by ``k1``; ``b`` interpolates between no length normalization and
    full normalization against the average eligible document length. Query terms
    are deduplicated so repeated user text cannot multiply a term's contribution.

    Readiness is tracked per tenant. A process starting over an existing catalog
    must rebuild before hybrid retrieval, making an absent projection explicit
    instead of silently behaving like dense-only retrieval.
    """

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 <= 0:
            raise ValueError("BM25 k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("BM25 b must be between 0 and 1")
        self.k1 = k1
        self.b = b
        self._documents: dict[str, _BM25Document] = {}
        self._ready_tenants: set[str] = set()
        self._lock = asyncio.Lock()

    async def upsert(
        self,
        catalog: CatalogVersion,
        chunks: tuple[IndexedChunk, ...],
    ) -> None:
        documents = [
            _BM25Document(
                chunk_id=chunk.chunk_id,
                content=chunk.public_content,
                tokens=tokenize(chunk.public_content),
                metadata=projection_metadata(catalog, chunk),
                acl=catalog.acl,
            )
            for chunk in chunks
        ]
        async with self._lock:
            for document in documents:
                self._documents[document.chunk_id] = document
            self._ready_tenants.add(catalog.tenant_id)

    async def query(
        self,
        query: str,
        *,
        acl_filter: ACLFilter,
        k: int,
        filters: dict[str, JsonValue],
    ) -> tuple[BM25Hit, ...]:
        if k <= 0:
            raise ValueError("BM25 result count must be positive")
        query_terms = frozenset(tokenize(query))
        async with self._lock:
            if acl_filter.tenant_id not in self._ready_tenants:
                raise IndexError("BM25 projection is not built for tenant")
            eligible = [
                document
                for document in self._documents.values()
                if self._allows(document, acl_filter, filters)
            ]
        if not query_terms or not eligible:
            return ()

        document_count = len(eligible)
        average_length = sum(len(document.tokens) for document in eligible) / document_count
        document_frequencies = {
            term: sum(term in document.tokens for document in eligible) for term in query_terms
        }
        hits: list[BM25Hit] = []
        for document in eligible:
            frequencies = Counter(document.tokens)
            score = 0.0
            for term in query_terms:
                term_frequency = frequencies[term]
                if term_frequency == 0:
                    continue
                document_frequency = document_frequencies[term]
                idf = math.log(
                    1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                length_ratio = len(document.tokens) / average_length if average_length else 0.0
                denominator = term_frequency + self.k1 * (1 - self.b + self.b * length_ratio)
                score += idf * (term_frequency * (self.k1 + 1)) / denominator
            if score > 0:
                hits.append(
                    BM25Hit(
                        chunk_id=document.chunk_id,
                        content=document.content,
                        metadata=document.metadata,
                        score=score,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return tuple(hits[:k])

    async def contains(self, chunk_id: str, tenant_id: str) -> bool:
        async with self._lock:
            document = self._documents.get(chunk_id)
            return document is not None and document.metadata.get("tenant_id") == tenant_id

    async def delete_document(self, tenant_id: str, document_id: str) -> None:
        await self._delete_matching(tenant_id=tenant_id, document_id=document_id)

    async def delete_document_version(
        self, tenant_id: str, document_id: str, document_version: str
    ) -> None:
        await self._delete_matching(
            tenant_id=tenant_id,
            document_id=document_id,
            document_version=document_version,
        )

    async def rebuild_tenant(self, tenant_id: str) -> None:
        """Clear a tenant and mark its empty projection as explicitly rebuilt."""
        async with self._lock:
            self._documents = {
                chunk_id: document
                for chunk_id, document in self._documents.items()
                if document.metadata.get("tenant_id") != tenant_id
            }
            self._ready_tenants.add(tenant_id)

    async def _delete_matching(
        self,
        *,
        tenant_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        async with self._lock:
            self._documents = {
                chunk_id: document
                for chunk_id, document in self._documents.items()
                if not (
                    document.metadata.get("tenant_id") == tenant_id
                    and document.metadata.get("document_id") == document_id
                    and (
                        document_version is None
                        or document.metadata.get("document_version") == document_version
                    )
                )
            }

    @staticmethod
    def _allows(
        document: _BM25Document,
        acl_filter: ACLFilter,
        filters: dict[str, JsonValue],
    ) -> bool:
        tenant_id = document.metadata.get("tenant_id")
        classification = document.metadata.get("classification")
        if not isinstance(tenant_id, str) or not isinstance(classification, str):
            return False
        if not acl_filter.allows(
            tenant_id=tenant_id,
            acl=document.acl,
            classification=classification,
        ):
            return False
        mapping = {
            "document_id": "document_id",
            "document_version": "document_version",
            "document_kind": "doc_document_kind",
            "rule_category": "rule_category",
        }
        for name, value in filters.items():
            if name not in mapping or not isinstance(value, (str, int, float, bool)):
                raise ValueError("unsupported retrieval filter")
            if document.metadata.get(mapping[name]) != value:
                return False
        return True
