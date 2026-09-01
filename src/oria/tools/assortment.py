"""T06 model tools backed by the typed assortment service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from oria.core.types import ToolResult, ValueModel
from oria.domain.assortment import (
    PUBLISH_CONSUMER_POLICY,
    SEND_NOTIFICATION_POLICY,
    SUBMIT_ASSORTMENT_POLICY,
    AssortmentService,
    PublishConsumerPlacementArgs,
    SendMerchantNotificationArgs,
    SubmitAssortmentArgs,
)

if TYPE_CHECKING:
    from oria.core.context import Context


class SubmitAssortmentToolResult(ValueModel):
    schema_version: Literal[1] = 1
    submission_id: str
    campaign_id: str
    submission_version: str
    status: Literal["pending", "submitted", "completed", "failed", "unknown"]
    item_count: int
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str


class PublishConsumerPlacementToolResult(ValueModel):
    schema_version: Literal[1] = 1
    placement_id: str
    campaign_id: str
    selection_version: str
    status: Literal["pending", "published", "failed", "unknown"]
    selected_item_count: int
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str


class SendMerchantNotificationToolResult(ValueModel):
    schema_version: Literal[1] = 1
    notification_id: str
    campaign_id: str
    result_version: str
    status: Literal["pending", "sent", "retrying", "dead_letter"]
    attempt_count: int
    execution_id: str
    idempotency_key: str
    request_idempotency_key: str


class SubmitAssortmentTool:
    name = "submit_assortment"
    schema_version = 1
    description = "Submit a server-validated confirmed-item set to the assortment adapter."
    json_schema: dict[str, Any] = SubmitAssortmentArgs.model_json_schema()
    result_schema: dict[str, Any] = SubmitAssortmentToolResult.model_json_schema(
        mode="serialization"
    )
    policy = SUBMIT_ASSORTMENT_POLICY

    def __init__(self, service: AssortmentService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        SubmitAssortmentArgs.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        result = await self._service.submit(SubmitAssortmentArgs.model_validate(params), ctx)
        data = SubmitAssortmentToolResult(
            submission_id=result.submission.assortment_submission_id,
            campaign_id=result.submission.campaign_id,
            submission_version=result.submission.submission_version,
            status=result.submission.status,
            item_count=len(result.enrollment_item_ids),
            execution_id=result.execution_id,
            idempotency_key=result.idempotency_key,
            request_idempotency_key=result.request_idempotency_key,
        )
        return _tool_result(self.name, data, result.execution_id, result.idempotency_key)


class PublishConsumerPlacementTool:
    name = "publish_consumer_placement"
    schema_version = 1
    description = "Publish an approved selection projection without exposing placement details."
    json_schema: dict[str, Any] = PublishConsumerPlacementArgs.model_json_schema()
    result_schema: dict[str, Any] = PublishConsumerPlacementToolResult.model_json_schema(
        mode="serialization"
    )
    policy = PUBLISH_CONSUMER_POLICY

    def __init__(self, service: AssortmentService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        PublishConsumerPlacementArgs.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        result = await self._service.publish_consumer_placement(
            PublishConsumerPlacementArgs.model_validate(params), ctx
        )
        data = PublishConsumerPlacementToolResult(
            placement_id=result.placement.consumer_placement_id,
            campaign_id=result.placement.campaign_id,
            selection_version=result.placement.selection_version,
            status=result.placement.status,
            selected_item_count=len(result.selected_item_ids),
            execution_id=result.execution_id,
            idempotency_key=result.idempotency_key,
            request_idempotency_key=result.request_idempotency_key,
        )
        return _tool_result(self.name, data, result.execution_id, result.idempotency_key)


class SendMerchantNotificationTool:
    name = "send_merchant_notification"
    schema_version = 1
    description = "Send a rendered result notification and return only a redacted delivery state."
    json_schema: dict[str, Any] = SendMerchantNotificationArgs.model_json_schema()
    result_schema: dict[str, Any] = SendMerchantNotificationToolResult.model_json_schema(
        mode="serialization"
    )
    policy = SEND_NOTIFICATION_POLICY

    def __init__(self, service: AssortmentService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        SendMerchantNotificationArgs.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        result = await self._service.send_merchant_notification(
            SendMerchantNotificationArgs.model_validate(params), ctx
        )
        data = SendMerchantNotificationToolResult(
            notification_id=result.notification.merchant_notification_id,
            campaign_id=result.notification.campaign_id,
            result_version=result.notification.result_version,
            status=result.notification.status,
            attempt_count=result.notification.attempt_count,
            execution_id=result.execution_id,
            idempotency_key=result.idempotency_key,
            request_idempotency_key=result.request_idempotency_key,
        )
        return _tool_result(self.name, data, result.execution_id, result.idempotency_key)


def _tool_result(
    tool_name: str,
    data: ValueModel,
    execution_id: str,
    idempotency_key: str,
) -> ToolResult:
    return ToolResult(
        ok=True,
        data=data.model_dump(mode="json"),
        execution_id=execution_id,
        idempotency_key=idempotency_key,
        trust_level="trusted_internal",
        provenance=f"oria://tool/{tool_name}/v1",
        data_classification="internal",
    )
