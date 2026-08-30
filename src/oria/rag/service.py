"""Document chunking, ingestion, authorized retrieval, deletion, and rebuild."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from oria.core.protocols import Embedder
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    CitationBlock,
    Doc,
    JsonValue,
    PolicyDecision,
    QueryFilters,
    ResourceRef,
)
from oria.rag.bm25 import BM25Index
from oria.rag.catalog import SQLiteKnowledgeCatalog
from oria.rag.errors import KnowledgeError
from oria.rag.index import ChromaIndex
from oria.rag.models import (
    CatalogVersion,
    DeletionResult,
    DocumentIngestRequest,
    IndexedChunk,
    IngestionResult,
    RebuildResult,
    RuleCategory,
)
from oria.rag.object_store import LocalObjectStore

if TYPE_CHECKING:
    from oria.core.context import Context

_CHUNKING_VERSION = "json-sections-v1"
_RULE_CATEGORIES: tuple[RuleCategory, ...] = (
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
)
_CATEGORY_LABELS = {
    "basic": "基础信息 活动模板 活动时间 活动类型",
    "recruitment_scope": "招商范围 行业类目 城市 黑白名单 报名系统 销售组织",
    "enrollment_policy": (
        "报名规则 客户圈选 商家自主 自动报名 关联活动报名 "
        "商品圈选 价格 类目 关键词 招后选品 策略 执行模式 完成条件"
    ),
    "benefit_policy": "优惠档位 基础档 膨胀档 固定金额 阶梯出资 币种 舍入 预算上限",
    "confirmation_policy": "确认规则 商家 销售 销售经理 超时动作",
    "merchant_material": "商家端素材 活动标题 头图 介绍 标签",
}
_RESERVED_FILTERS = frozenset(
    {"tenant_id", "acl", "allowed_subject_ids", "allowed_roles", "classification"}
)


class LocalKnowledgeService:
    """Coordinate ObjectStore truth, SQLite catalog, and Chroma projection."""

    def __init__(
        self,
        *,
        catalog: SQLiteKnowledgeCatalog,
        objects: LocalObjectStore,
        index: ChromaIndex,
        embedder: Embedder,
        embedding_profile: str,
        bm25_index: BM25Index | None = None,
    ) -> None:
        self._catalog = catalog
        self._objects = objects
        self._index = index
        self._embedder = embedder
        self._embedding_profile = embedding_profile
        self._bm25_index = bm25_index

    async def ingest(self, request: DocumentIngestRequest, ctx: Context) -> IngestionResult:
        await _authorize("knowledge:write", request.document_id, ctx)
        _validate_document_metadata(request)
        data = request.content.encode("utf-8")
        content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        object_key = (
            f"{ctx.tenant_id}/{request.document_id}/{request.version}/"
            f"{content_hash.removeprefix('sha256:')}"
        )
        object_ref = self._objects.put_bytes(object_key, data, ctx)
        chunks = _chunk_document(request, content_hash, ctx.tenant_id)
        run_id = f"ing_{uuid.uuid4().hex}"
        idempotent = await self._catalog.begin_ingestion(
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            request=request,
            content_hash=content_hash,
            object_ref=object_ref,
            chunking_version=_CHUNKING_VERSION,
            embedding_profile=self._embedding_profile,
        )
        catalog = _catalog_from_request(
            ctx.tenant_id,
            request,
            content_hash,
            object_ref,
            self._embedding_profile,
        )
        try:
            embeddings = await self._embedder.embed([chunk.public_content for chunk in chunks], ctx)
            await self._index.upsert(catalog, chunks, embeddings)
            if self._bm25_index is not None:
                await self._bm25_index.upsert(catalog, chunks)
        except BaseException:
            if not idempotent:
                await self._catalog.finish_ingestion(ctx.tenant_id, run_id, success=False)
            raise
        if not idempotent:
            await self._catalog.finish_ingestion(ctx.tenant_id, run_id, success=True)
        superseded_versions = await self._catalog.list_superseded_versions(
            ctx.tenant_id, request.document_id
        )
        for version in superseded_versions:
            await self._index.delete_document_version_all_projections(
                ctx.tenant_id,
                request.document_id,
                version,
            )
            if self._bm25_index is not None:
                await self._bm25_index.delete_document_version(
                    ctx.tenant_id,
                    request.document_id,
                    version,
                )
        return IngestionResult(
            document_id=request.document_id,
            document_version=request.version,
            content_hash=content_hash,
            object_ref=object_ref,
            chunk_count=len(chunks),
            idempotent=idempotent,
        )

    async def rebuild(self, ctx: Context) -> RebuildResult:
        await _authorize("knowledge:write", "rebuild", ctx)
        versions = await self._catalog.list_active_versions(ctx.tenant_id)
        await self._index.delete_tenant(ctx.tenant_id)
        if self._bm25_index is not None:
            await self._bm25_index.rebuild_tenant(ctx.tenant_id)
        chunk_count = 0
        for catalog in versions:
            data = self._objects.read_bytes(catalog.object_ref, ctx)
            observed = f"sha256:{hashlib.sha256(data).hexdigest()}"
            if observed != catalog.content_hash:
                raise KnowledgeError("catalog object content failed integrity verification")
            request = _request_from_catalog(catalog, data)
            chunks = _chunk_document(request, catalog.content_hash, catalog.tenant_id)
            embeddings = await self._embedder.embed([chunk.public_content for chunk in chunks], ctx)
            await self._index.upsert(catalog, chunks, embeddings)
            if self._bm25_index is not None:
                await self._bm25_index.upsert(catalog, chunks)
            chunk_count += len(chunks)
        return RebuildResult(document_versions=len(versions), chunk_count=chunk_count)

    async def delete(self, document_id: str, ctx: Context) -> DeletionResult:
        await _authorize("knowledge:delete", document_id, ctx)
        active_versions = tuple(
            version
            for version in await self._catalog.list_active_versions(ctx.tenant_id)
            if version.document_id == document_id
        )
        known_versions = await self._catalog.list_document_versions(ctx.tenant_id, document_id)
        await self._index.delete_document_all_projections(ctx.tenant_id, document_id)
        if self._bm25_index is not None:
            await self._bm25_index.delete_document(ctx.tenant_id, document_id)
        for version in known_versions:
            self._objects.delete_ref(version.object_ref, ctx)
        if active_versions:
            await self._catalog.mark_document_deleted(ctx.tenant_id, document_id)
        return DeletionResult(document_id=document_id, deleted_versions=len(active_versions))

    async def citation_exists(self, citation: CitationBlock, ctx: Context) -> bool:
        try:
            await self.load_public_chunk(citation, ctx)
            return True
        except (KnowledgeError, ValueError, ValidationError):
            return False

    async def load_public_chunk(self, citation: CitationBlock, ctx: Context) -> str:
        """Return source-derived public content, never the mutable vector projection copy."""
        selected, _ = await self._load_cited_chunk(citation, ctx)
        return selected.public_content

    async def load_rule_section(
        self, citation: CitationBlock, ctx: Context, *, require_rule: bool = True
    ) -> tuple[RuleCategory | None, dict[str, JsonValue]]:
        selected, data = await self._load_cited_chunk(citation, ctx)
        if selected.rule_category is None:
            if require_rule:
                raise KnowledgeError("cited chunk is not a campaign rule section")
            return None, {"content": selected.public_content}
        payload = _json_object(data)
        section = payload.get(selected.rule_category)
        if not isinstance(section, dict):
            raise KnowledgeError("campaign rule section is malformed")
        return selected.rule_category, cast(dict[str, JsonValue], section)

    async def _load_cited_chunk(
        self, citation: CitationBlock, ctx: Context
    ) -> tuple[IndexedChunk, bytes]:
        decision = await _authorize("rule:read", citation.document_id, ctx)
        acl_filter = decision.require_acl_filter()
        catalog = await self._catalog.get_active_version(
            ctx.tenant_id, citation.document_id, citation.document_version
        )
        if catalog is None or not acl_filter.allows(
            tenant_id=catalog.tenant_id,
            acl=catalog.acl,
            classification=catalog.data_classification,
        ):
            raise KnowledgeError("cited knowledge is unavailable")
        if not await self._index.contains_versioned_chunk(
            citation.chunk_id,
            tenant_id=ctx.tenant_id,
            document_id=citation.document_id,
            document_version=citation.document_version,
            content_hash=catalog.content_hash,
        ):
            raise KnowledgeError("cited knowledge projection is unavailable")
        data = self._objects.read_bytes(catalog.object_ref, ctx)
        observed_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if observed_hash != catalog.content_hash:
            raise KnowledgeError("cited knowledge failed integrity verification")
        request = _request_from_catalog(catalog, data)
        chunks = _chunk_document(request, catalog.content_hash, catalog.tenant_id)
        selected = next((chunk for chunk in chunks if chunk.chunk_id == citation.chunk_id), None)
        if selected is None:
            raise KnowledgeError("cited chunk does not match the source document")
        return selected, data


class AuthorizedChromaRetriever:
    """Apply policy-derived tenant/ACL filters before and after vector recall."""

    def __init__(
        self,
        *,
        catalog: SQLiteKnowledgeCatalog,
        index: ChromaIndex,
        embedder: Embedder,
        knowledge: LocalKnowledgeService,
    ) -> None:
        self._catalog = catalog
        self._index = index
        self._embedder = embedder
        self._knowledge = knowledge

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
        decision = await _authorize("rule:read", "knowledge", ctx)
        acl_filter = decision.require_acl_filter()
        effective_filters = acl_filter.and_query_filters(query_filters)
        if not acl_filter.classifications:
            return []
        filters = {
            name: value
            for name, value in effective_filters.attributes.items()
            if name not in _RESERVED_FILTERS
        }
        if acl_filter.tenant_id != ctx.tenant_id:
            raise PermissionError("knowledge read is not authorized")
        query_embedding = (await self._embedder.embed([query], ctx))[0]
        hits = await self._index.query(
            query_embedding,
            acl_filter=acl_filter,
            k=min(k * 5, 100),
            filters=filters,
        )
        documents: list[Doc] = []
        for hit in hits:
            document_id = hit.metadata.get("document_id")
            version = hit.metadata.get("document_version")
            if not isinstance(document_id, str) or not isinstance(version, str):
                continue
            catalog = await self._catalog.get_active_version(ctx.tenant_id, document_id, version)
            if (
                catalog is None
                or not acl_filter.allows(
                    tenant_id=catalog.tenant_id,
                    acl=catalog.acl,
                    classification=catalog.data_classification,
                )
                or hit.metadata.get("tenant_id") != ctx.tenant_id
                or hit.metadata.get("content_hash") != catalog.content_hash
            ):
                continue
            citation = CitationBlock(
                document_id=document_id,
                document_version=version,
                chunk_id=hit.chunk_id,
            )
            try:
                source_content = await self._knowledge.load_public_chunk(citation, ctx)
            except (KnowledgeError, ValueError, ValidationError):
                continue
            metadata: dict[str, JsonValue] = {
                **catalog.metadata,
                "document_id": document_id,
                "chunk_id": hit.chunk_id,
            }
            category = hit.metadata.get("rule_category")
            if isinstance(category, str):
                metadata["rule_category"] = category
            documents.append(
                Doc(
                    id=hit.chunk_id,
                    version=version,
                    tenant_id=ctx.tenant_id,
                    content=source_content,
                    metadata=metadata,
                    score=1.0 / (1.0 + hit.distance),
                    source_uri=catalog.source_uri,
                    acl=catalog.acl,
                    trust_level="untrusted_data",
                    provenance=catalog.source_uri,
                    data_classification=catalog.data_classification,
                )
            )
            if len(documents) == k:
                break
        return documents


class AuthorizedBM25Retriever:
    """Apply the same policy and source-of-truth checks to lexical recall."""

    def __init__(
        self,
        *,
        catalog: SQLiteKnowledgeCatalog,
        index: BM25Index,
        knowledge: LocalKnowledgeService,
    ) -> None:
        self._catalog = catalog
        self._index = index
        self._knowledge = knowledge

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
        decision = await _authorize("rule:read", "knowledge", ctx)
        acl_filter = decision.require_acl_filter()
        effective_filters = acl_filter.and_query_filters(query_filters)
        if not acl_filter.classifications:
            return []
        filters = {
            name: value
            for name, value in effective_filters.attributes.items()
            if name not in _RESERVED_FILTERS
        }
        if acl_filter.tenant_id != ctx.tenant_id:
            raise PermissionError("knowledge read is not authorized")
        hits = await self._index.query(
            query,
            acl_filter=acl_filter,
            k=min(k * 5, 100),
            filters=filters,
        )
        documents: list[Doc] = []
        for hit in hits:
            document_id = hit.metadata.get("document_id")
            version = hit.metadata.get("document_version")
            if not isinstance(document_id, str) or not isinstance(version, str):
                continue
            catalog = await self._catalog.get_active_version(ctx.tenant_id, document_id, version)
            if (
                catalog is None
                or not acl_filter.allows(
                    tenant_id=catalog.tenant_id,
                    acl=catalog.acl,
                    classification=catalog.data_classification,
                )
                or hit.metadata.get("tenant_id") != ctx.tenant_id
                or hit.metadata.get("content_hash") != catalog.content_hash
            ):
                continue
            citation = CitationBlock(
                document_id=document_id,
                document_version=version,
                chunk_id=hit.chunk_id,
            )
            try:
                source_content = await self._knowledge.load_public_chunk(citation, ctx)
            except (KnowledgeError, ValueError, ValidationError):
                continue
            metadata: dict[str, JsonValue] = {
                **catalog.metadata,
                "document_id": document_id,
                "chunk_id": hit.chunk_id,
            }
            category = hit.metadata.get("rule_category")
            if isinstance(category, str):
                metadata["rule_category"] = category
            documents.append(
                Doc(
                    id=hit.chunk_id,
                    version=version,
                    tenant_id=ctx.tenant_id,
                    content=source_content,
                    metadata=metadata,
                    score=hit.score,
                    source_uri=catalog.source_uri,
                    acl=catalog.acl,
                    trust_level="untrusted_data",
                    provenance=catalog.source_uri,
                    data_classification=catalog.data_classification,
                )
            )
            if len(documents) == k:
                break
        return documents


def _chunk_document(
    request: DocumentIngestRequest,
    content_hash: str,
    tenant_id: str,
) -> tuple[IndexedChunk, ...]:
    data = request.content.encode("utf-8")
    if request.metadata.get("document_kind") == "campaign_rules":
        payload = _json_object(data)
        if set(payload) != set(_RULE_CATEGORIES):
            raise ValueError("campaign rule document must contain exactly six categories")
        chunks: list[IndexedChunk] = []
        for category in _RULE_CATEGORIES:
            section = payload[category]
            if not isinstance(section, dict):
                raise ValueError("campaign rule category must be an object")
            raw = json.dumps(section, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            public = _public_section(category, cast(dict[str, JsonValue], section))
            chunks.append(
                IndexedChunk(
                    chunk_id=_chunk_id(
                        tenant_id,
                        request.document_id,
                        request.version,
                        category,
                        raw,
                    ),
                    document_id=request.document_id,
                    document_version=request.version,
                    content_hash=content_hash,
                    public_content=public,
                    rule_category=category,
                )
            )
        return tuple(chunks)
    paragraphs = [part.strip() for part in request.content.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [request.content]
    return tuple(
        IndexedChunk(
            chunk_id=_chunk_id(
                tenant_id,
                request.document_id,
                request.version,
                str(index),
                content,
            ),
            document_id=request.document_id,
            document_version=request.version,
            content_hash=content_hash,
            public_content=content,
        )
        for index, content in enumerate(paragraphs)
    )


def _public_section(category: RuleCategory, section: dict[str, JsonValue]) -> str:
    public = dict(section)
    if category == "recruitment_scope":
        for name in (
            "allowlist_merchant_ids",
            "denylist_merchant_ids",
            "sales_org_scope",
        ):
            value = public.pop(name, [])
            count = len(value) if isinstance(value, (list, tuple)) else 0
            public[f"{name}_ref"] = f"restricted:{count}"
    payload = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    label = _CATEGORY_LABELS[category]
    if category == "benefit_policy":
        tier_rules = public.get("tier_rules")
        if isinstance(tier_rules, (list, tuple)) and any(
            isinstance(rule, dict) and rule.get("funding_type") == "discount_rate"
            for rule in tier_rules
        ):
            label = f"{label} 折扣率"
    return f"{label}\n{payload}"


def _chunk_id(
    tenant_id: str,
    document_id: str,
    version: str,
    section: str,
    content: str,
) -> str:
    value = f"{tenant_id}\0{document_id}\0{version}\0{section}\0{content}".encode()
    return f"chk_{hashlib.sha256(value).hexdigest()[:32]}"


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("document is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("document JSON root must be an object")
    return cast(dict[str, Any], value)


def _validate_document_metadata(request: DocumentIngestRequest) -> None:
    if request.metadata.get("document_kind") != "campaign_rules":
        return
    required = {
        "rule_type",
        "effective_from",
        "effective_to",
        "priority",
        "supersedes",
        "template_ref",
    }
    if not required.issubset(request.metadata):
        raise ValueError("campaign rule metadata is incomplete")
    start = _aware_datetime(request.metadata.get("effective_from"))
    end = _aware_datetime(request.metadata.get("effective_to"))
    priority = request.metadata.get("priority")
    if start >= end or not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("campaign rule metadata is invalid")


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("rule effective time must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("rule effective time must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("rule effective time must include a timezone")
    return parsed


def _catalog_from_request(
    tenant_id: str,
    request: DocumentIngestRequest,
    content_hash: str,
    object_ref: str,
    embedding_profile: str,
) -> CatalogVersion:
    return CatalogVersion(
        tenant_id=tenant_id,
        document_id=request.document_id,
        version=request.version,
        source_uri=request.source_uri,
        owner_ref=request.owner_ref,
        data_classification=request.data_classification,
        content_hash=content_hash,
        object_ref=object_ref,
        acl=request.acl,
        metadata=request.metadata,
        chunking_version=_CHUNKING_VERSION,
        embedding_profile=embedding_profile,
    )


def _request_from_catalog(catalog: CatalogVersion, data: bytes) -> DocumentIngestRequest:
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KnowledgeError("catalog object is not valid UTF-8") from exc
    return DocumentIngestRequest(
        document_id=catalog.document_id,
        version=catalog.version,
        source_uri=catalog.source_uri,
        owner_ref=catalog.owner_ref,
        data_classification=cast(Any, catalog.data_classification),
        content=content,
        acl=catalog.acl,
        metadata=catalog.metadata,
    )


async def _authorize(action: str, resource_id: str, ctx: Context) -> PolicyDecision:
    decision = await ctx.policy.authorize(
        AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=action,
            resource=ResourceRef(
                resource_type="knowledge",
                resource_id=resource_id,
                tenant_id=ctx.tenant_id,
            ),
            context=AuthorizationContext(correlation_id=ctx.run_id),
        ),
        ctx,
    )
    if not decision.allow:
        raise PermissionError("knowledge operation is not authorized")
    return decision
