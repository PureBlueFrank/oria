"""Chroma projection adapter; never treated as the document truth source."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import chromadb
from chromadb.config import Settings

from oria.core.types import ACLFilter, JsonValue
from oria.rag.errors import IndexError
from oria.rag.models import CatalogVersion, IndexedChunk, IndexHit

_COLLECTION_PREFIX = "oria_document_chunks_v1"


class ChromaIndex:
    """Store a tenant/ACL-filterable, fully rebuildable chunk projection."""

    def __init__(
        self,
        path: Path,
        *,
        projection_id: str,
        embedding_dimension: int,
    ) -> None:
        if not projection_id:
            raise ValueError("projection identity must be non-empty")
        if embedding_dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self._path = path.resolve(strict=False)
        identity = f"{projection_id}\0{embedding_dimension}".encode()
        digest = hashlib.sha256(identity).hexdigest()[:20]
        self._collection_name = f"{_COLLECTION_PREFIX}_{digest}"
        self._projection_id = projection_id
        self._embedding_dimension = embedding_dimension
        self._client: Any = None
        self._collection: Any = None

    async def __aenter__(self) -> ChromaIndex:
        if self._path.exists() and self._path.is_symlink():
            raise ValueError("Chroma path cannot be a symlink")
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = await asyncio.to_thread(
            chromadb.PersistentClient,
            path=self._path,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = await asyncio.to_thread(
            self._client.get_or_create_collection,
            self._collection_name,
            embedding_function=None,
            metadata={
                "hnsw:space": "cosine",
                "oria:projection_id": self._projection_id,
                "oria:embedding_dimension": self._embedding_dimension,
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)

    async def upsert(
        self,
        catalog: CatalogVersion,
        chunks: tuple[IndexedChunk, ...],
        embeddings: list[list[float]],
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunk and embedding counts differ")
        if not chunks:
            return
        metadatas = [projection_metadata(catalog, chunk) for chunk in chunks]
        try:
            await asyncio.to_thread(
                self._collection.upsert,
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=embeddings,
                documents=[chunk.public_content for chunk in chunks],
                metadatas=metadatas,
            )
        except Exception as exc:
            raise IndexError("vector projection write failed") from exc

    async def query(
        self,
        embedding: list[float],
        *,
        acl_filter: ACLFilter,
        k: int,
        filters: dict[str, JsonValue],
    ) -> tuple[IndexHit, ...]:
        where = self._where(acl_filter, filters)
        try:
            result = await asyncio.to_thread(
                self._collection.query,
                query_embeddings=[embedding],
                n_results=k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            hits: list[IndexHit] = []
            for chunk_id, content, metadata, distance in zip(
                ids, documents, metadatas, distances, strict=True
            ):
                if not isinstance(content, str) or not isinstance(metadata, dict):
                    raise IndexError("vector projection returned malformed content")
                hits.append(
                    IndexHit(
                        chunk_id=str(chunk_id),
                        content=content,
                        metadata=cast(dict[str, JsonValue], metadata),
                        distance=float(distance),
                    )
                )
            return tuple(hits)
        except IndexError:
            raise
        except Exception as exc:
            raise IndexError("vector projection query failed") from exc

    async def contains(self, chunk_id: str, tenant_id: str) -> bool:
        try:
            result = await asyncio.to_thread(
                self._collection.get,
                ids=[chunk_id],
                where={"tenant_id": {"$eq": tenant_id}},
                include=["metadatas"],
            )
            ids = result.get("ids")
            return isinstance(ids, list) and ids == [chunk_id]
        except Exception as exc:
            raise IndexError("vector projection lookup failed") from exc

    async def contains_versioned_chunk(
        self,
        chunk_id: str,
        *,
        tenant_id: str,
        document_id: str,
        document_version: str,
        content_hash: str,
    ) -> bool:
        """Verify a citation projection against its authoritative version identity."""
        where = {
            "$and": [
                {"tenant_id": {"$eq": tenant_id}},
                {"document_id": {"$eq": document_id}},
                {"document_version": {"$eq": document_version}},
                {"content_hash": {"$eq": content_hash}},
            ]
        }
        try:
            result = await asyncio.to_thread(
                self._collection.get,
                ids=[chunk_id],
                where=where,
                include=["metadatas"],
            )
            ids = result.get("ids")
            return isinstance(ids, list) and ids == [chunk_id]
        except Exception as exc:
            raise IndexError("vector projection citation lookup failed") from exc

    async def delete_document(self, tenant_id: str, document_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._collection.delete,
                where={
                    "$and": [
                        {"tenant_id": {"$eq": tenant_id}},
                        {"document_id": {"$eq": document_id}},
                    ]
                },
            )
        except Exception as exc:
            raise IndexError("vector projection deletion failed") from exc

    async def delete_document_all_projections(self, tenant_id: str, document_id: str) -> None:
        """Remove one document from every Oria projection in this Chroma store."""
        where = {
            "$and": [
                {"tenant_id": {"$eq": tenant_id}},
                {"document_id": {"$eq": document_id}},
            ]
        }
        try:
            collections = await asyncio.to_thread(self._client.list_collections)
            for collection in collections:
                if not collection.name.startswith(f"{_COLLECTION_PREFIX}_"):
                    continue
                await asyncio.to_thread(collection.delete, where=where)
        except Exception as exc:
            raise IndexError("vector projection deletion failed") from exc

    async def delete_document_version_all_projections(
        self,
        tenant_id: str,
        document_id: str,
        document_version: str,
    ) -> None:
        """Remove one superseded document version from every Oria projection."""
        where = {
            "$and": [
                {"tenant_id": {"$eq": tenant_id}},
                {"document_id": {"$eq": document_id}},
                {"document_version": {"$eq": document_version}},
            ]
        }
        try:
            collections = await asyncio.to_thread(self._client.list_collections)
            for collection in collections:
                if not collection.name.startswith(f"{_COLLECTION_PREFIX}_"):
                    continue
                await asyncio.to_thread(collection.delete, where=where)
        except Exception as exc:
            raise IndexError("vector projection version deletion failed") from exc

    async def delete_tenant(self, tenant_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._collection.delete,
                where={"tenant_id": {"$eq": tenant_id}},
            )
        except Exception as exc:
            raise IndexError("vector projection reset failed") from exc

    @staticmethod
    def _where(
        acl_filter: ACLFilter,
        filters: dict[str, JsonValue],
    ) -> dict[str, Any]:
        acl_terms: list[dict[str, Any]] = [
            {"acl_public": {"$eq": True}},
        ]
        acl_terms.extend(
            {"acl_subjects": {"$contains": subject_id}}
            for subject_id in acl_filter.allowed_subject_ids
        )
        acl_terms.extend({"acl_roles": {"$contains": role}} for role in acl_filter.allowed_roles)
        terms: list[dict[str, Any]] = [
            {"tenant_id": {"$eq": acl_filter.tenant_id}},
            {"$or": acl_terms},
            {"classification": {"$in": list(acl_filter.classifications)}},
        ]
        mapping = {
            "document_id": "document_id",
            "document_version": "document_version",
            "rule_category": "rule_category",
        }
        for name, value in filters.items():
            if name not in mapping or not isinstance(value, (str, int, float, bool)):
                raise ValueError("unsupported retrieval filter")
            terms.append({mapping[name]: {"$eq": value}})
        return {"$and": terms}


def projection_metadata(catalog: CatalogVersion, chunk: IndexedChunk) -> dict[str, Any]:
    """Build the shared ACL/version metadata used by dense and lexical projections."""
    metadata: dict[str, Any] = {
        "tenant_id": catalog.tenant_id,
        "document_id": catalog.document_id,
        "document_version": catalog.version,
        "content_hash": catalog.content_hash,
        "source_uri": catalog.source_uri,
        "classification": catalog.data_classification,
        "acl_public": not (catalog.acl.allowed_subject_ids or catalog.acl.allowed_roles),
        "acl_subjects": list(catalog.acl.allowed_subject_ids) or ["__none__"],
        "acl_roles": list(catalog.acl.allowed_roles) or ["__none__"],
    }
    if chunk.rule_category is not None:
        metadata["rule_category"] = chunk.rule_category
    for key, value in catalog.metadata.items():
        if isinstance(value, (str, int, float, bool)):
            metadata[f"doc_{key}"] = value
        elif value is not None:
            metadata[f"doc_{key}"] = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
    return metadata
