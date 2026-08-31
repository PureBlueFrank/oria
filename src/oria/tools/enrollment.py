"""Medium-risk enrollment aggregation and coupon-link tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.domain.enrollment import (
    CouponLinkService,
    EnrollmentService,
    LinkCouponBatchArgs,
    LinkCouponBatchResult,
    UpsertEnrollmentItemsArgs,
    UpsertEnrollmentItemsResult,
)

if TYPE_CHECKING:
    from oria.core.context import Context


class UpsertEnrollmentItemsTool:
    name = "upsert_enrollment_items"
    schema_version = 1
    description = "Idempotently aggregate one merchant or auto source into enrollment items."
    json_schema: dict[str, Any] = UpsertEnrollmentItemsArgs.model_json_schema()
    result_schema: dict[str, Any] = UpsertEnrollmentItemsResult.model_json_schema(
        mode="serialization"
    )
    policy = ToolPolicy(
        risk_level="medium",
        side_effect=True,
        timeout_seconds=30,
        retry_policy=RetryPolicy(max_attempts=1),
        idempotency_scope="campaign_id:item_business_keys:source",
        required_action="enrollment:item:write",
        resource_type="campaign",
        approval_mode="none",
    )

    def __init__(self, service: EnrollmentService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        UpsertEnrollmentItemsArgs.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        result = await self._service.upsert_items(
            UpsertEnrollmentItemsArgs.model_validate(params), ctx
        )
        return ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            execution_id=result.execution_id,
            idempotency_key=result.idempotency_key,
            trust_level="trusted_internal",
            provenance="oria://tool/upsert_enrollment_items/v1",
            data_classification="restricted_derivative",
        )


class LinkCouponBatchTool:
    name = "link_coupon_batch"
    schema_version = 1
    description = "Atomically link confirmed enrollment items to one ready coupon batch."
    json_schema: dict[str, Any] = LinkCouponBatchArgs.model_json_schema()
    result_schema: dict[str, Any] = LinkCouponBatchResult.model_json_schema(mode="serialization")
    policy = ToolPolicy(
        risk_level="medium",
        side_effect=True,
        timeout_seconds=30,
        retry_policy=RetryPolicy(max_attempts=1),
        idempotency_scope="enrollment_item_id:coupon_batch_id:benefit_tier",
        required_action="enrollment:coupon:link",
        resource_type="coupon_batch",
        approval_mode="none",
    )

    def __init__(self, service: CouponLinkService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        LinkCouponBatchArgs.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        result = await self._service.link(LinkCouponBatchArgs.model_validate(params), ctx)
        return ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            execution_id=result.execution_id,
            idempotency_key=result.idempotency_key,
            trust_level="trusted_internal",
            provenance="oria://tool/link_coupon_batch/v1",
            data_classification="internal",
        )
