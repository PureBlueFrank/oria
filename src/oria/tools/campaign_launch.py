"""T04 write tools backed exclusively by the typed campaign launch service."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.domain.launch import (
    MATERIALIZE_TOOL_POLICY,
    PUBLISH_TOOL_POLICY,
    CampaignDraftSpec,
    CampaignLaunchService,
)
from oria.tools.models import (
    LaunchApprovalParams,
    LaunchApprovalResult,
    LaunchChildExecutionResult,
    MaterializeCouponBatchParams,
    PersistCampaignDraftParams,
    PersistCampaignDraftResult,
    PublishRecruitmentParams,
)

if TYPE_CHECKING:
    from oria.core.context import Context


def _execution_id() -> str:
    return f"tool_{uuid.uuid4().hex}"


class PersistCampaignDraftTool:
    name = "persist_campaign_draft"
    schema_version = 1
    description = "Validate and persist a local campaign draft without external side effects."
    json_schema: dict[str, Any] = PersistCampaignDraftParams.model_json_schema()
    result_schema: dict[str, Any] = PersistCampaignDraftResult.model_json_schema(
        mode="serialization"
    )
    policy = ToolPolicy(
        risk_level="medium",
        side_effect=False,
        timeout_seconds=15,
        retry_policy=RetryPolicy(max_attempts=1),
        required_action="campaign:draft:write",
        resource_type="campaign",
        approval_mode="none",
    )

    def __init__(self, service: CampaignLaunchService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        PersistCampaignDraftParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = PersistCampaignDraftParams.model_validate(params)
        snapshot = await ctx.rule_snapshots.get(request.rule_snapshot_id, ctx)
        draft = await self._service.persist_campaign_draft(
            CampaignDraftSpec(
                campaign_id=request.campaign_id,
                coupon_batch_id=request.coupon_batch_id,
                recruitment_publication_id=request.recruitment_publication_id,
                material_version=request.material_version,
                compensation_policy_version=request.compensation_policy_version,
            ),
            snapshot,
            ctx,
        )
        result = PersistCampaignDraftResult.from_draft(draft)
        return ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            execution_id=_execution_id(),
            trust_level="trusted_internal",
            provenance="oria://tool/persist_campaign_draft/v1",
            data_classification="restricted_derivative",
        )


class LaunchApprovalTool:
    name = "launch_approval"
    schema_version = 1
    description = "Request approval for one immutable, schema-controlled LaunchPlan."
    json_schema: dict[str, Any] = LaunchApprovalParams.model_json_schema()
    result_schema: dict[str, Any] = LaunchApprovalResult.model_json_schema(mode="serialization")
    policy = ToolPolicy(
        risk_level="high",
        side_effect=False,
        timeout_seconds=15,
        retry_policy=RetryPolicy(max_attempts=1),
        required_action="approval:launch:request",
        resource_type="approval",
        approval_mode="none",
    )

    def __init__(self, service: CampaignLaunchService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        LaunchApprovalParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = LaunchApprovalParams.model_validate(params)
        binding = await self._service.request_launch_approval(
            draft=request.draft,
            materialize_args=request.materialize_args,
            publish_args=request.publish_args,
            checkpoint_id=request.checkpoint_id,
            expires_at=request.expires_at,
            ctx=ctx,
        )
        if binding.approval.status != "pending":
            raise RuntimeError("new launch approval must be pending")
        result = LaunchApprovalResult(
            approval_id=binding.approval.approval_id,
            approval_status="pending",
            checkpoint_id=binding.approval.checkpoint_id,
            plan=binding.plan,
        )
        return ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            execution_id=_execution_id(),
            trust_level="trusted_internal",
            provenance="oria://tool/launch_approval/v1",
            data_classification="internal",
        )


class MaterializeCouponBatchTool:
    name = "materialize_coupon_batch"
    schema_version = 1
    description = "Materialize an approved coupon batch through the execution ledger."
    json_schema: dict[str, Any] = MaterializeCouponBatchParams.model_json_schema()
    result_schema: dict[str, Any] = LaunchChildExecutionResult.model_json_schema(
        mode="serialization"
    )
    policy = MATERIALIZE_TOOL_POLICY

    def __init__(self, service: CampaignLaunchService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        MaterializeCouponBatchParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = MaterializeCouponBatchParams.model_validate(params)
        execution = await self._service.materialize_coupon_batch(
            args=request.args,
            plan=request.plan,
            approval_id=request.approval_id,
            checkpoint_id=request.checkpoint_id,
            ctx=ctx,
        )
        return _execution_result(execution)


class PublishRecruitmentTool:
    name = "publish_recruitment"
    schema_version = 1
    description = "Publish an approved recruitment campaign through the execution ledger."
    json_schema: dict[str, Any] = PublishRecruitmentParams.model_json_schema()
    result_schema: dict[str, Any] = LaunchChildExecutionResult.model_json_schema(
        mode="serialization"
    )
    policy = PUBLISH_TOOL_POLICY

    def __init__(self, service: CampaignLaunchService) -> None:
        self._service = service

    def validate_params(self, params: dict[str, Any]) -> None:
        PublishRecruitmentParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = PublishRecruitmentParams.model_validate(params)
        execution = await self._service.publish_recruitment(
            args=request.args,
            plan=request.plan,
            approval_id=request.approval_id,
            checkpoint_id=request.checkpoint_id,
            ctx=ctx,
        )
        return _execution_result(execution)


def _execution_result(execution: Any) -> ToolResult:
    result = LaunchChildExecutionResult(
        execution_id=execution.execution_id,
        tool_name=execution.tool_name,
        status=execution.status,
        idempotency_key=execution.idempotency_key,
        canonical_args_hash=execution.canonical_args_hash,
    )
    return ToolResult(
        ok=True,
        data=result.model_dump(mode="json"),
        execution_id=execution.execution_id,
        idempotency_key=execution.idempotency_key,
        trust_level="trusted_internal",
        provenance=f"oria://tool/{execution.tool_name}/v1",
        data_classification="internal",
    )
