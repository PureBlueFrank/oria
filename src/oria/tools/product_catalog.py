"""Low-risk deterministic product catalog query tool."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.domain.products import ProductQueryService
from oria.tools.models import QueryEligibleProductsParams, QueryEligibleProductsResult

if TYPE_CHECKING:
    from oria.core.context import Context


class QueryEligibleProductsTool:
    name = "query_eligible_products"
    schema_version = 1
    description = "Query one stable catalog page and apply frozen deterministic product rules."
    json_schema: dict[str, Any] = QueryEligibleProductsParams.model_json_schema()
    result_schema: dict[str, Any] = QueryEligibleProductsResult.model_json_schema(
        mode="serialization"
    )
    policy = ToolPolicy(
        risk_level="low",
        side_effect=False,
        timeout_seconds=15,
        retry_policy=RetryPolicy(max_attempts=2),
        required_action="product:read",
        resource_type="campaign",
        approval_mode="none",
    )

    def __init__(self, service: ProductQueryService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        QueryEligibleProductsParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = QueryEligibleProductsParams.model_validate(params)
        result = await self._service.query(request, ctx)
        return ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            execution_id=f"tool_{uuid.uuid4().hex}",
            trust_level="trusted_internal",
            provenance="oria://tool/query_eligible_products/v1",
            data_classification="restricted_derivative",
        )
