"""Strict read-only tools for Scenario B attribution research."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import Field, model_validator

from oria.analytics.models import (
    ActivityWindow,
    AnalyticsEvidence,
    AnalyticsPeriod,
    FunnelPoint,
    MarketSegmentOverview,
)
from oria.analytics.query import AnalyticsQueryStore
from oria.core.types import (
    CitationBlock,
    QueryFilters,
    RetryPolicy,
    ToolPolicy,
    ToolResult,
    ValueModel,
)
from oria.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.core.protocols import Retriever

FunnelDimension = Literal["event_date", "region", "category"]
MarketDimension = Literal["region", "category"]

_ANALYTICS_POLICY = ToolPolicy(
    risk_level="low",
    side_effect=False,
    timeout_seconds=15,
    retry_policy=RetryPolicy(max_attempts=1),
    required_action="analytics:read",
    resource_type="analytics_fact",
    approval_mode="none",
)
_HISTORY_POLICY = ToolPolicy(
    risk_level="low",
    side_effect=False,
    timeout_seconds=15,
    retry_policy=RetryPolicy(max_attempts=1),
    required_action="document:read",
    resource_type="knowledge_document",
    approval_mode="none",
)


class QueryFunnelParams(ValueModel):
    period: AnalyticsPeriod
    dimensions: tuple[FunnelDimension, ...] = Field(min_length=1, max_length=3)
    region: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_unique_dimensions(self) -> Self:
        if len(self.dimensions) != len(set(self.dimensions)):
            raise ValueError("funnel dimensions must be unique")
        return self


class QueryFunnelResult(ValueModel):
    schema_version: Literal[1] = 1
    dimensions: tuple[FunnelDimension, ...]
    rows: tuple[FunnelPoint, ...]
    evidence: AnalyticsEvidence


class DrillDownParams(ValueModel):
    period: AnalyticsPeriod
    dimension: Literal["region", "category"]
    value: str = Field(min_length=1, max_length=128)
    group_by: tuple[FunnelDimension, ...] = Field(
        default=("event_date",), min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def validate_grouping(self) -> Self:
        if len(self.group_by) != len(set(self.group_by)):
            raise ValueError("drill-down group_by dimensions must be unique")
        if self.dimension in self.group_by:
            raise ValueError("drill-down dimension cannot also be grouped")
        return self


class DrillDownResult(ValueModel):
    schema_version: Literal[1] = 1
    dimension: Literal["region", "category"]
    value: str
    group_by: tuple[FunnelDimension, ...]
    rows: tuple[FunnelPoint, ...]
    evidence: AnalyticsEvidence


class QueryActivityParams(ValueModel):
    period: AnalyticsPeriod
    category: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_scope(self) -> Self:
        if self.category is None and self.merchant_id is None:
            raise ValueError("category or merchant_id is required")
        return self


class QueryActivityResult(ValueModel):
    schema_version: Literal[1] = 1
    activities: tuple[ActivityWindow, ...]
    evidence: AnalyticsEvidence


class QueryMarketOverviewParams(ValueModel):
    period: AnalyticsPeriod
    comparison: Literal["previous_period", "year_over_year"]
    dimensions: tuple[MarketDimension, ...] = Field(
        default=("region", "category"), min_length=1, max_length=2
    )
    region: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_unique_dimensions(self) -> Self:
        if len(self.dimensions) != len(set(self.dimensions)):
            raise ValueError("market dimensions must be unique")
        return self


class QueryMarketOverviewResult(ValueModel):
    schema_version: Literal[1] = 1
    comparison: Literal["previous_period", "year_over_year"]
    segments: tuple[MarketSegmentOverview, ...]
    evidence: AnalyticsEvidence


class SearchHistoryExperienceParams(ValueModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=10)


class HistoryExperienceHit(ValueModel):
    citation: CitationBlock
    excerpt: str = Field(min_length=1, max_length=2000)
    score: float = Field(ge=0)
    source_uri: str = Field(min_length=1)
    provenance: str = Field(min_length=1)


class SearchHistoryExperienceResult(ValueModel):
    schema_version: Literal[1] = 1
    query: str
    hits: tuple[HistoryExperienceHit, ...]


class QueryFunnelTool:
    name = "query_funnel"
    schema_version = 1
    description = "Query tenant-scoped funnel metrics over a bounded period and fixed dimensions."
    json_schema: dict[str, Any] = QueryFunnelParams.model_json_schema()
    result_schema: dict[str, Any] = QueryFunnelResult.model_json_schema(mode="serialization")
    policy = _ANALYTICS_POLICY

    def __init__(self, store: AnalyticsQueryStore) -> None:
        self._store = store

    def validate_params(self, params: dict[str, Any]) -> None:
        QueryFunnelParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = QueryFunnelParams.model_validate(params)
        rows, evidence = await asyncio.to_thread(
            self._store.query_funnel,
            tenant_id=ctx.tenant_id,
            period=request.period,
            dimensions=request.dimensions,
            region=request.region,
            category=request.category,
        )
        data = QueryFunnelResult(dimensions=request.dimensions, rows=rows, evidence=evidence)
        return _tool_result(self.name, data, evidence.provenance)


class DrillDownTool:
    name = "drill_down"
    schema_version = 1
    description = "Drill into one allowed funnel dimension without accepting arbitrary SQL."
    json_schema: dict[str, Any] = DrillDownParams.model_json_schema()
    result_schema: dict[str, Any] = DrillDownResult.model_json_schema(mode="serialization")
    policy = _ANALYTICS_POLICY

    def __init__(self, store: AnalyticsQueryStore) -> None:
        self._store = store

    def validate_params(self, params: dict[str, Any]) -> None:
        DrillDownParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = DrillDownParams.model_validate(params)
        rows, evidence = await asyncio.to_thread(
            self._store.drill_down,
            tenant_id=ctx.tenant_id,
            period=request.period,
            dimension=request.dimension,
            value=request.value,
            group_by=request.group_by,
        )
        data = DrillDownResult(
            dimension=request.dimension,
            value=request.value,
            group_by=request.group_by,
            rows=rows,
            evidence=evidence,
        )
        return _tool_result(self.name, data, evidence.provenance)


class QueryActivityTool:
    name = "query_activity"
    schema_version = 1
    description = "Query overlapping tenant activities by category, merchant, or both."
    json_schema: dict[str, Any] = QueryActivityParams.model_json_schema()
    result_schema: dict[str, Any] = QueryActivityResult.model_json_schema(mode="serialization")
    policy = _ANALYTICS_POLICY

    def __init__(self, store: AnalyticsQueryStore) -> None:
        self._store = store

    def validate_params(self, params: dict[str, Any]) -> None:
        QueryActivityParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = QueryActivityParams.model_validate(params)
        activities, evidence = await asyncio.to_thread(
            self._store.query_activity,
            tenant_id=ctx.tenant_id,
            period=request.period,
            category=request.category,
            merchant_id=request.merchant_id,
        )
        data = QueryActivityResult(activities=activities, evidence=evidence)
        return _tool_result(self.name, data, evidence.provenance)


class QueryMarketOverviewTool:
    name = "query_market_overview"
    schema_version = 1
    description = "Compare tenant market metrics with the previous period or prior year."
    json_schema: dict[str, Any] = QueryMarketOverviewParams.model_json_schema()
    result_schema: dict[str, Any] = QueryMarketOverviewResult.model_json_schema(
        mode="serialization"
    )
    policy = _ANALYTICS_POLICY

    def __init__(self, store: AnalyticsQueryStore) -> None:
        self._store = store

    def validate_params(self, params: dict[str, Any]) -> None:
        QueryMarketOverviewParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = QueryMarketOverviewParams.model_validate(params)
        segments, evidence = await asyncio.to_thread(
            self._store.query_market_overview,
            tenant_id=ctx.tenant_id,
            period=request.period,
            comparison=request.comparison,
            dimensions=request.dimensions,
            region=request.region,
            category=request.category,
        )
        data = QueryMarketOverviewResult(
            comparison=request.comparison,
            segments=segments,
            evidence=evidence,
        )
        return _tool_result(self.name, data, evidence.provenance)


class SearchHistoryExperienceTool:
    name = "search_history_experience"
    schema_version = 1
    description = "Search authorized historical attribution experience as untrusted cited data."
    json_schema: dict[str, Any] = SearchHistoryExperienceParams.model_json_schema()
    result_schema: dict[str, Any] = SearchHistoryExperienceResult.model_json_schema(
        mode="serialization"
    )
    policy = _HISTORY_POLICY

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    def validate_params(self, params: dict[str, Any]) -> None:
        SearchHistoryExperienceParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = SearchHistoryExperienceParams.model_validate(params)
        docs = await self._retriever.retrieve(
            request.query,
            ctx,
            k=request.limit,
            query_filters=QueryFilters(attributes={"document_kind": "attribution_history"}),
        )
        hits: list[HistoryExperienceHit] = []
        for doc in docs:
            if doc.tenant_id != ctx.tenant_id:
                raise PermissionError("retriever returned cross-tenant history")
            if doc.metadata.get("document_kind") != "attribution_history":
                raise ValueError("retriever returned a non-history document")
            document_id = doc.metadata.get("document_id")
            if not isinstance(document_id, str) or not document_id:
                raise ValueError("history document identity is unavailable")
            hits.append(
                HistoryExperienceHit(
                    citation=CitationBlock(
                        document_id=document_id,
                        document_version=doc.version,
                        chunk_id=doc.id,
                    ),
                    excerpt=doc.content[:2000],
                    score=doc.score,
                    source_uri=doc.source_uri,
                    provenance=doc.provenance,
                )
            )
        data = SearchHistoryExperienceResult(query=request.query, hits=tuple(hits))
        return ToolResult(
            ok=True,
            data=data.model_dump(mode="json"),
            execution_id=_execution_id(),
            trust_level="untrusted_data",
            provenance="oria://tool/search_history_experience/v1",
            data_classification="internal",
        )


def build_attribution_tool_registry(
    store: AnalyticsQueryStore,
    retriever: Retriever,
) -> ToolRegistry:
    """Build the sealed five-tool registry consumed by the V0.4 research graph."""
    registry = ToolRegistry(
        allowlist=frozenset(
            {
                "query_funnel",
                "drill_down",
                "query_activity",
                "query_market_overview",
                "search_history_experience",
            }
        )
    )
    registry.register(QueryFunnelTool(store))
    registry.register(DrillDownTool(store))
    registry.register(QueryActivityTool(store))
    registry.register(QueryMarketOverviewTool(store))
    registry.register(SearchHistoryExperienceTool(retriever))
    registry.seal()
    return registry


def _execution_id() -> str:
    return f"tool_{uuid.uuid4().hex}"


def _tool_result(tool_name: str, data: ValueModel, provenance: str) -> ToolResult:
    return ToolResult(
        ok=True,
        data=data.model_dump(mode="json"),
        execution_id=_execution_id(),
        trust_level="trusted_internal",
        provenance=f"oria://tool/{tool_name}/v1#{provenance.rsplit('/', maxsplit=1)[-1]}",
        data_classification="internal",
    )
