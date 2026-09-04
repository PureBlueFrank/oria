"""Trusted local executor for Scenario A CLI start, resume, approval, and Mock events."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import Field

from oria.agent.models import CampaignProposal
from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import Context, RuntimeServices
from oria.core.runtime import build_runtime
from oria.core.types import JsonValue, NodeResult, Principal, ValueModel
from oria.data import initialize_data
from oria.domain.launch import CampaignDraftSpec
from oria.orchestrator.checkpoint import checkpoint_config
from oria.orchestrator.scenario_a import (
    DefaultScenarioAWorkflowService,
    ScenarioAWorkflowRequest,
    initial_scenario_a_state,
    scenario_a_request,
)
from oria.permission.local import LOCAL_TENANT_ID, local_cli_executor
from oria.presentation.workflow import (
    ApprovalSummary,
    ConfirmationProgress,
    CouponBatchSummary,
    EnrollmentItemDetail,
    MerchantExclusionCount,
    MerchantMatch,
    MerchantMatches,
    NotificationMessage,
    PlacementSummary,
    SelectionDecisionDetail,
    SelectionSummary,
    WorkflowViewModel,
    proposal_rule_summary,
)
from oria.rag.demo import demo_rule_document
from oria.tools.models import QueryMerchantsResult

FIXTURE_NOW = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
MOCK_MERCHANT_ADAPTER = "mock-merchant"
MOCK_MERCHANT_SUBJECT = "mock-merchant-adapter"
MOCK_SELECTION_ADAPTER = "mock-selection"
MOCK_SELECTION_SUBJECT = "mock-selection-adapter"


class LocalWorkflowResult(ValueModel):
    ok: bool = True
    thread_id: str
    status: Literal["waiting", "completed"]
    interrupts: tuple[dict[str, JsonValue], ...] = ()
    detail: dict[str, JsonValue] = Field(default_factory=dict)
    view: WorkflowViewModel = Field(exclude=True)


def _principal(
    subject_id: str, *roles: str, kind: Literal["human", "service"] = "human"
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=LOCAL_TENANT_ID,
        kind=kind,
        roles=roles,
        authn_method="trusted-local-workflow",
    )


def campaign_admin() -> Principal:
    return _principal("local-campaign-admin", "operator", "campaign_admin")


def launch_approver() -> Principal:
    return _principal("local-launch-approver", "launch_approver")


def consumer_publish_approver() -> Principal:
    return _principal("local-consumer-publish-approver", "consumer_publish_approver")


def integration_executor() -> Principal:
    return _principal("local-integration-executor", "integration_adapter", kind="service")


def _integration_actor() -> Principal:
    return _principal("local-integration-actor", "integration_adapter")


def _trusted_actors(extra: tuple[Principal, ...] = ()) -> tuple[Principal, ...]:
    return (
        campaign_admin(),
        launch_approver(),
        consumer_publish_approver(),
        _integration_actor(),
        *extra,
    )


async def _runtime(
    config: ResolvedRuntimeConfig,
    *,
    confirmation_assignments: dict[str, str] | None = None,
    extra_actors: tuple[Principal, ...] = (),
) -> RuntimeServices:
    return await build_runtime(
        config,
        clock=lambda: FIXTURE_NOW,
        trusted_actors=_trusted_actors(extra_actors),
        trusted_executors=(local_cli_executor(), integration_executor()),
        confirmation_assignments=confirmation_assignments,
    )


def _ctx(
    runtime: RuntimeServices,
    *,
    actor: Principal,
    executor: Principal,
    thread_id: str,
    run_suffix: str,
) -> Context:
    return runtime.new_context(
        actor=actor,
        executor=executor,
        session_id=f"scenario-a:{thread_id}",
        thread_id=thread_id,
        run_id=f"scenario-a:{thread_id}:{run_suffix}",
        correlation_id=f"scenario-a:{thread_id}",
    )


def _scenario(runtime: RuntimeServices) -> DefaultScenarioAWorkflowService:
    service = runtime.domain.scenario_a
    if not isinstance(service, DefaultScenarioAWorkflowService):
        raise RuntimeError("trusted Scenario A service is unavailable")
    return service


def _graph(runtime: RuntimeServices) -> Any:
    return runtime.agents.get("scenario_a")


def _interrupts(value: object) -> tuple[Interrupt, ...]:
    if isinstance(value, StateSnapshot):
        return value.interrupts
    if isinstance(value, dict):
        raw = value.get("__interrupt__", ())
        if isinstance(raw, (list, tuple)) and all(isinstance(item, Interrupt) for item in raw):
            return tuple(raw)
    return ()


def _public_interrupt(interruption: Interrupt) -> dict[str, JsonValue]:
    value = interruption.value
    if not isinstance(value, dict):
        raise RuntimeError("workflow produced an invalid interrupt payload")
    visible = {
        key: item
        for key, item in value.items()
        if key
        in {
            "approval_id",
            "confirmation_task_id",
            "kind",
            "target_role",
            "wait_id",
        }
    }
    return cast(
        dict[str, JsonValue],
        {"interrupt_id": interruption.id, **visible},
    )


_STAGES = (
    "生成规则与活动草案",
    "招商发布审批",
    "券批次物化与招商发布",
    "报名与商品圈选",
    "动态业务确认",
    "报名商品关联券批次",
    "提交并等待招后选品",
    "C 端发布审批",
    "C 端投放",
    "通知商家并闭环",
)

_INTERRUPT_STAGES = {
    "launch_approval": 2,
    "enrollment_window": 4,
    "business_confirmation": 5,
    "selection_event": 7,
    "consumer_publish_approval": 8,
    "workflow_handoff": 6,
}

_RESULT_STAGES = {
    "draft": 1,
    "launch_approval": 2,
    "launch": 3,
    "enrollment_prepared": 4,
    "auto_enrollment": 4,
    "merchant_enrollment": 4,
    "enrollment_join": 4,
    "coupon_link": 6,
    "assortment_submission": 7,
    "selection_wait": 7,
    "selection": 7,
    "consumer_publish_approval": 8,
    "consumer_publish": 9,
    "merchant_notifications": 10,
}

_ROLE_LABELS = {
    "merchant": "商家",
    "sales": "销售",
    "sales_manager": "销售经理",
    "campaign_admin": "活动管理员",
}

_EXCLUSION_LABELS = {
    "inactive": "商家未启用",
    "category_mismatch": "类目不符",
    "city_mismatch": "城市不符",
    "not_allowlisted": "名单策略未通过",
    "denylisted": "名单策略未通过",
    "enrollment_system_mismatch": "报名系统不符",
    "sales_org_mismatch": "销售组织不符",
}


def _state_values(value: object) -> Mapping[str, object]:
    if isinstance(value, StateSnapshot):
        return cast(Mapping[str, object], value.values)
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _state_results(values: Mapping[str, object]) -> dict[str, NodeResult]:
    raw = values.get("results")
    if not isinstance(raw, Mapping):
        return {}
    results: dict[str, NodeResult] = {}
    for key, item in raw.items():
        if not isinstance(key, str):
            continue
        try:
            results[key] = item if isinstance(item, NodeResult) else NodeResult.model_validate(item)
        except ValueError:
            continue
    return results


def _terminal_outcome(
    results: Mapping[str, NodeResult], interruptions: tuple[dict[str, JsonValue], ...]
) -> Literal["completed", "rejected", "failed", "reconciliation_required"] | None:
    failures = [result for result in results.values() if result.status == "failed"]
    error_codes = tuple(result.error.code for result in failures if result.error is not None)
    if any("reject" in code for code in error_codes):
        return "rejected"
    if any(
        _contains_outcome(
            result.updates,
            {"unknown", "reconciliation", "reconciliation_required", "compensation_pending"},
        )
        for result in results.values()
    ) or any("reconciliation" in code or "unknown" in code for code in error_codes):
        return "reconciliation_required"
    if failures:
        return "failed"
    if interruptions:
        return None
    if results.get("merchant_notifications", NodeResult(status="waiting")).status == "completed":
        return "completed"
    return "failed"


def _contains_outcome(value: object, outcomes: set[str]) -> bool:
    if isinstance(value, str):
        return value in outcomes
    if isinstance(value, Mapping):
        return any(_contains_outcome(item, outcomes) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_outcome(item, outcomes) for item in value)
    return False


def _active_stage(
    results: Mapping[str, NodeResult],
    interruptions: tuple[dict[str, JsonValue], ...],
    terminal: str | None,
) -> int:
    if terminal == "completed":
        return len(_STAGES)
    if interruptions:
        kind = interruptions[0].get("kind")
        if isinstance(kind, str):
            return _INTERRUPT_STAGES.get(kind, 1)
    for result in results.values():
        code = result.error.code if result.error is not None else ""
        if result.status == "failed" and code == "launch_rejected":
            return 2
        if result.status == "failed" and code == "consumer_publish_rejected":
            return 8
    failed_stages = [
        _RESULT_STAGES.get(key.split(":", maxsplit=1)[0], 1)
        for key, result in results.items()
        if result.status == "failed"
    ]
    if failed_stages:
        return max(failed_stages)
    completed_stages = [
        _RESULT_STAGES.get(key.split(":", maxsplit=1)[0], 1)
        for key, result in results.items()
        if result.status == "completed"
    ]
    return max(completed_stages, default=1)


def _proposal_and_merchants(
    results: Mapping[str, NodeResult],
) -> tuple[CampaignProposal | None, QueryMerchantsResult | None]:
    draft = results.get("draft")
    if draft is None:
        return None, None
    try:
        proposal = CampaignProposal.model_validate(draft.updates.get("proposal"))
    except ValueError:
        proposal = None
    try:
        merchants = QueryMerchantsResult.model_validate(draft.updates.get("merchant_result"))
    except ValueError:
        merchants = None
    return proposal, merchants


def _merchant_summary(
    proposal: CampaignProposal | None, merchants: QueryMerchantsResult | None
) -> tuple[MerchantMatches, tuple[MerchantExclusionCount, ...]]:
    recommendations = {
        item.merchant_id: item for item in (proposal.recommended_merchants if proposal else ())
    }
    items: tuple[MerchantMatch, ...] = ()
    if merchants is not None:
        items = tuple(
            MerchantMatch(
                merchant_id=candidate.merchant_id,
                display_name=candidate.display_name,
                llm_rank=(
                    recommendations[candidate.merchant_id].rank
                    if candidate.merchant_id in recommendations
                    else None
                ),
                recommendation_reason=(
                    recommendations[candidate.merchant_id].reason
                    if candidate.merchant_id in recommendations
                    else "通过硬资格筛选, 未进入本次推荐排序"
                ),
            )
            for candidate in sorted(
                merchants.candidates,
                key=lambda item: (
                    recommendations[item.merchant_id].rank
                    if item.merchant_id in recommendations
                    else 10_000,
                    item.merchant_id,
                ),
            )
        )
    matches = MerchantMatches(
        matched_count=(merchants.eligible_count if merchants else len(recommendations)),
        evaluated_count=(merchants.evaluated_count if merchants else len(recommendations)),
        items=items,
    )
    aggregated: Counter[str] = Counter()
    if merchants is not None:
        for reason, count in merchants.exclusion_reason_counts.items():
            if reason != "eligible":
                aggregated[_EXCLUSION_LABELS.get(reason, "其他资格条件未通过")] += count
    exclusions = tuple(
        MerchantExclusionCount(reason=reason, count=count)
        for reason, count in sorted(aggregated.items())
    )
    # Future per-merchant disclosure belongs in a PolicyEngine-authorized,
    # field-redacted MerchantEligibilityDisplayProjection. Never derive it here.
    return matches, exclusions


def _confirmation_progress(
    results: Mapping[str, NodeResult], interruptions: tuple[dict[str, JsonValue], ...]
) -> ConfirmationProgress | None:
    current = next(
        (item for item in interruptions if item.get("kind") == "business_confirmation"), None
    )
    if current is None:
        return None
    task_id = current.get("confirmation_task_id")
    task_lists: list[object] = []
    joined = results.get("enrollment_join")
    if joined is not None:
        task_lists.append(joined.updates.get("confirmation_tasks"))
    task_lists.extend(
        result.updates.get("confirmation_tasks")
        for key, result in results.items()
        if key.startswith("confirmation_decision:")
    )
    tasks = next(
        (items for items in reversed(task_lists) if isinstance(items, (list, tuple)) and items),
        (),
    )
    typed = [item for item in tasks if isinstance(item, Mapping)]
    active = next((item for item in typed if item.get("confirmation_task_id") == task_id), None)
    if active is None:
        return None
    sequence = active.get("sequence")
    role = active.get("subject_type")
    if not isinstance(sequence, int) or not isinstance(role, str):
        return None
    following = next((item for item in typed if item.get("sequence") == sequence + 1), None)
    next_role = following.get("subject_type") if following is not None else None
    return ConfirmationProgress(
        current_level=sequence,
        total_levels=len(typed),
        current_role=_ROLE_LABELS.get(role, role),
        next_role=(_ROLE_LABELS.get(next_role, next_role) if isinstance(next_role, str) else None),
    )


def _selection_summary(
    values: Mapping[str, object],
    results: Mapping[str, NodeResult],
    detail: Mapping[str, JsonValue],
) -> SelectionSummary:
    assortment = results.get("assortment_submission")
    submission_version: str | None = None
    submitted_count = 0
    if assortment is not None:
        submission = assortment.updates.get("submission")
        if isinstance(submission, Mapping) and isinstance(
            submission.get("submission_version"), str
        ):
            submission_version = cast(str, submission["submission_version"])
        item_ids = assortment.updates.get("enrollment_item_ids")
        if isinstance(item_ids, (list, tuple)):
            submitted_count = len(item_ids)
    approval = results.get("consumer_publish_approval")
    projection = approval.updates.get("selection_summary") if approval is not None else None
    if not isinstance(projection, Mapping):
        projection = {}
    selected_products = projection.get("selected_products", ())
    if not isinstance(selected_products, (list, tuple)):
        selected_products = ()
    selected_count = _nonnegative_int(projection.get("selected_count"))
    rejected_count = _nonnegative_int(projection.get("rejected_count"))
    received_count = selected_count + rejected_count
    if received_count == 0 and "selection_version" in detail and submitted_count:
        received_count = 1
        if detail.get("decision") == "selected":
            selected_count = 1
        elif detail.get("decision") == "rejected":
            rejected_count = 1
    if results.get("selection") is not None and received_count == 0:
        received_count = submitted_count
    request = _scenario_request_from_values(values)
    placement_scope = None
    if request is not None:
        placement_scope = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in sorted(request.placement_spec.items())
        )
    return SelectionSummary(
        submission_version=submission_version,
        submitted_count=submitted_count,
        received_count=min(received_count, submitted_count),
        pending_count=max(submitted_count - received_count, 0),
        selected_count=selected_count,
        rejected_count=rejected_count,
        selected_products=tuple(item for item in selected_products if isinstance(item, str)),
        coupon_linked_count=_nonnegative_int(projection.get("coupon_linked_count")),
        placement_scope=placement_scope,
    )


def _enrollment_items(results: Mapping[str, NodeResult]) -> tuple[EnrollmentItemDetail, ...]:
    joined = results.get("enrollment_join")
    raw = joined.updates.get("enrollment_items") if joined is not None else None
    if not isinstance(raw, (list, tuple)):
        return ()
    projected: list[EnrollmentItemDetail] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        sources = item.get("sources")
        try:
            projected.append(
                EnrollmentItemDetail.model_validate(
                    {
                        "merchant_id": item.get("merchant_id"),
                        "product_ref": item.get("product_ref"),
                        "product_version": item.get("product_version"),
                        "sources": (
                            tuple(value for value in sources if isinstance(value, str))
                            if isinstance(sources, (list, tuple))
                            else ()
                        ),
                        "status": item.get("status"),
                    }
                )
            )
        except ValueError:
            continue
    return tuple(projected)


def _coupon_batch(results: Mapping[str, NodeResult]) -> CouponBatchSummary | None:
    launch = results.get("launch")
    raw = launch.updates.get("coupon_batch") if launch is not None else None
    if not isinstance(raw, Mapping):
        return None
    face_values = raw.get("face_values")
    try:
        return CouponBatchSummary.model_validate(
            {
                "coupon_batch_id": raw.get("coupon_batch_id"),
                "face_values": (
                    tuple(value for value in face_values if isinstance(value, str))
                    if isinstance(face_values, (list, tuple))
                    else ()
                ),
                "quantity": raw.get("quantity"),
                "quantity_note": raw.get("quantity_note"),
                "budget_cap": raw.get("budget_cap"),
                "currency": raw.get("currency"),
                "status": raw.get("status"),
            }
        )
    except ValueError:
        return None


def _selection_decisions(
    results: Mapping[str, NodeResult],
) -> tuple[SelectionDecisionDetail, ...]:
    approval = results.get("consumer_publish_approval")
    raw = approval.updates.get("selection_decisions") if approval is not None else None
    if not isinstance(raw, (list, tuple)):
        return ()
    projected: list[SelectionDecisionDetail] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            projected.append(
                SelectionDecisionDetail.model_validate(
                    {
                        "merchant_id": item.get("merchant_id"),
                        "product_ref": item.get("product_ref"),
                        "product_version": item.get("product_version"),
                        "decision": item.get("decision"),
                        "reason": item.get("reason"),
                    }
                )
            )
        except ValueError:
            continue
    return tuple(projected)


def _placement(results: Mapping[str, NodeResult]) -> PlacementSummary | None:
    for result_key in ("consumer_publish", "consumer_publish_approval"):
        result = results.get(result_key)
        raw = result.updates.get("placement_display") if result is not None else None
        if not isinstance(raw, Mapping):
            continue
        selected = raw.get("selected_products")
        try:
            return PlacementSummary.model_validate(
                {
                    "channel": raw.get("channel"),
                    "region": raw.get("region"),
                    "content_example": raw.get("content_example"),
                    "selected_products": (
                        tuple(value for value in selected if isinstance(value, str))
                        if isinstance(selected, (list, tuple))
                        else ()
                    ),
                    "status": raw.get("status"),
                }
            )
        except ValueError:
            continue
    return None


def _notification_messages(
    results: Mapping[str, NodeResult],
) -> tuple[NotificationMessage, ...]:
    result = results.get("merchant_notifications")
    raw = result.updates.get("notification_messages") if result is not None else None
    if not isinstance(raw, (list, tuple)):
        return ()
    projected: list[NotificationMessage] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            projected.append(
                NotificationMessage.model_validate(
                    {
                        "merchant_id": item.get("merchant_id"),
                        "channel": item.get("channel"),
                        "status": item.get("status"),
                        "message": item.get("message"),
                    }
                )
            )
        except ValueError:
            continue
    return tuple(projected)


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _scenario_request_from_values(values: Mapping[str, object]) -> ScenarioAWorkflowRequest | None:
    try:
        return scenario_a_request(cast(Any, values))
    except (KeyError, TypeError, ValueError):
        return None


def _pending_copy(
    thread_id: str,
    interruptions: tuple[dict[str, JsonValue], ...],
    confirmation: ConfirmationProgress | None,
) -> tuple[str, str | None, ApprovalSummary | None]:
    if not interruptions:
        return "流程已停止, 当前没有待处理动作。", None, None
    interruption = interruptions[0]
    kind = interruption.get("kind")
    quoted_thread = shlex.quote(thread_id)
    if kind in {"launch_approval", "consumer_publish_approval"}:
        approval_id = interruption.get("approval_id")
        if not isinstance(approval_id, str):
            return "等待审批, 但审批标识不可用。", None, None
        description = "招商发布审批" if kind == "launch_approval" else "C 端发布审批"
        command = (
            f"oria approval approve --thread-id {quoted_thread} "
            f"--approval-id {shlex.quote(approval_id)}"
        )
        summary = ApprovalSummary(
            kind=cast(Literal["launch_approval", "consumer_publish_approval"], kind),
            approval_id=approval_id,
            status="pending",
            description=description,
        )
        if kind == "launch_approval":
            return (
                "规则、候选范围及活动/券草案已冻结, 等待招商发布审批。",
                command,
                summary,
            )
        return (
            "入选商品、券关联与投放范围已冻结, 等待 C 端发布审批。",
            command,
            summary,
        )
    if kind == "enrollment_window":
        command = f"oria mock window-close --thread-id {quoted_thread} --source-event-id <event-id>"
        return "招商已发布, 等待报名/关窗; 当前正在接收报名。", command, None
    if kind == "business_confirmation":
        task_id = interruption.get("confirmation_task_id")
        if not isinstance(task_id, str):
            return "等待业务确认, 但确认任务标识不可用。", None, None
        next_role = (
            f", 下一位为{confirmation.next_role}" if confirmation and confirmation.next_role else ""
        )
        progress = (
            f"当前第 {confirmation.current_level}/{confirmation.total_levels} 级, "
            f"由{confirmation.current_role}确认{next_role}。"
            if confirmation
            else "等待当前业务角色确认。"
        )
        command = (
            f"oria workflow resume --thread-id {quoted_thread} "
            f"--confirmation-task-id {shlex.quote(task_id)} --decision confirm"
        )
        return progress, command, None
    if kind == "selection_event":
        command = (
            f"oria mock selection-complete --thread-id {quoted_thread} "
            "--source-event-id <event-id> --selection-version <version>"
        )
        return "选品批次已提交, 正在等待剩余选品结果。", command, None
    return "流程等待受信执行器继续处理。", None, None


def workflow_view_from_state(
    thread_id: str,
    value: object,
    interruptions: tuple[dict[str, JsonValue], ...],
    detail: Mapping[str, JsonValue],
) -> WorkflowViewModel:
    """Build the read-only human projection from public checkpoint state values."""

    values = _state_values(value)
    results = _state_results(values)
    terminal = _terminal_outcome(results, interruptions)
    stage = _active_stage(results, interruptions, terminal)
    request = _scenario_request_from_values(values)
    effective_at = request.effective_at.isoformat() if request is not None else "—"
    proposal, merchants = _proposal_and_merchants(results)
    matches, exclusions = _merchant_summary(proposal, merchants)
    confirmation = _confirmation_progress(results, interruptions)
    pending_action, next_command, approval = _pending_copy(thread_id, interruptions, confirmation)
    if terminal == "completed":
        pending_action = "全部十个业务步骤已完成。"
    elif terminal == "rejected":
        pending_action = "流程已按拒绝决定终止。"
    elif terminal == "failed":
        pending_action = "流程因执行错误终止。"
    elif terminal == "reconciliation_required":
        pending_action = "请先完成人工对账, 再决定补偿或恢复。"
    completed_count = stage if terminal == "completed" else max(stage - 1, 0)
    return WorkflowViewModel(
        thread_id=thread_id,
        flow_name="招商活动自动化",
        stage_index=stage,
        stage_total=len(_STAGES),
        current_stage=_STAGES[stage - 1],
        completed_steps=_STAGES[:completed_count],
        pending_action=pending_action,
        next_command=next_command if terminal is None else None,
        rule_summary=proposal_rule_summary(proposal, effective_at),
        merchant_matches=matches,
        merchant_exclusion_summary=exclusions,
        approval_summary=approval,
        confirmation_progress=confirmation,
        selection_summary=_selection_summary(values, results, detail),
        enrollment_items=_enrollment_items(results),
        coupon_batch=_coupon_batch(results),
        selection_decisions=_selection_decisions(results),
        placement=_placement(results),
        notification_messages=_notification_messages(results),
        terminal_outcome=cast(Any, terminal),
    )


def _result(thread_id: str, value: object, **detail: JsonValue) -> LocalWorkflowResult:
    interruptions = _interrupts(value)
    public_interrupts = tuple(_public_interrupt(item) for item in interruptions)
    return LocalWorkflowResult(
        thread_id=thread_id,
        status="waiting" if interruptions else "completed",
        interrupts=public_interrupts,
        detail=detail,
        view=workflow_view_from_state(thread_id, value, public_interrupts, detail),
    )


def default_request(
    *,
    campaign_id: str,
    user_request: str,
) -> ScenarioAWorkflowRequest:
    return ScenarioAWorkflowRequest(
        user_request=user_request,
        effective_at=FIXTURE_NOW,
        draft=CampaignDraftSpec(
            campaign_id=campaign_id,
            coupon_batch_id=f"{campaign_id}-coupon",
            recruitment_publication_id=f"{campaign_id}-recruitment",
            material_version="synthetic-material-v1",
            compensation_policy_version="synthetic-no-auto-compensation-v1",
        ),
        enrollment_mode="hybrid",
        product_limit=1,
        circle_run_id=f"{campaign_id}-circle-v1",
        coupon_link_idempotency_key=f"{campaign_id}:coupon-link:v1",
        assortment_policy_ref="synthetic-assortment-policy",
        assortment_policy_version="1.0.0",
        assortment_idempotency_key=f"{campaign_id}:assortment:v1",
        selection_expected_version=1,
        placement_spec={"channel": "synthetic-home-feed", "region": "east-china"},
        placement_idempotency_key=f"{campaign_id}:placement:v1",
        notification_template_id="selection-result-v1",
        notification_channel="mock-im",
        notification_idempotency_prefix=f"{campaign_id}:notification:v1",
        approval_expires_at=FIXTURE_NOW + timedelta(days=2),
        external_wait_expires_at=FIXTURE_NOW + timedelta(days=40),
    )


async def start_local_workflow(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    campaign_id: str,
    user_request: str,
    workflow_request: ScenarioAWorkflowRequest | None = None,
) -> LocalWorkflowResult:
    await initialize_data(config)
    runtime = await _runtime(config)
    try:
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="start",
        )
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        request = workflow_request or default_request(
            campaign_id=campaign_id, user_request=user_request
        )
        if request.draft.campaign_id != campaign_id:
            raise ValueError("workflow request campaign does not match the requested campaign")
        value = await _graph(runtime).ainvoke(
            initial_scenario_a_state(
                request,
                meta={
                    "tenant_id": ctx.tenant_id,
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "job_id": None,
                    "requester_subject_id": ctx.actor.subject_id,
                },
            ),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, campaign_id=campaign_id)
    finally:
        await runtime.aclose()


async def _snapshot(
    runtime: RuntimeServices,
    *,
    thread_id: str,
) -> tuple[Context, StateSnapshot]:
    ctx = _ctx(
        runtime,
        actor=campaign_admin(),
        executor=local_cli_executor(),
        thread_id=thread_id,
        run_suffix="inspect",
    )
    snapshot = await _graph(runtime).aget_state(checkpoint_config(ctx))
    if not snapshot.values:
        raise LookupError("workflow thread is unavailable")
    return ctx, snapshot


async def inspect_local_workflow(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
) -> LocalWorkflowResult:
    """Read one tenant-qualified checkpoint and build the existing human view."""

    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        return _result(thread_id, snapshot)
    finally:
        await runtime.aclose()


def _single_interrupt(snapshot: StateSnapshot, kind: str) -> dict[str, JsonValue]:
    matches = [
        item.value
        for item in snapshot.interrupts
        if isinstance(item.value, dict) and item.value.get("kind") == kind
    ]
    if len(matches) != 1:
        raise ValueError(f"workflow is not waiting for {kind}")
    return cast(dict[str, JsonValue], matches[0])


async def decide_local_approval(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    approval_id: str,
    decision: Literal["approve", "reject"],
    reason: str | None,
    decision_actor: Principal | None = None,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        admin_ctx, snapshot = await _snapshot(runtime, thread_id=thread_id)
        pending = [
            kind
            for kind in ("launch_approval", "consumer_publish_approval")
            if any(
                isinstance(item.value, dict) and item.value.get("kind") == kind
                for item in snapshot.interrupts
            )
        ]
        if len(pending) != 1:
            raise ValueError("workflow is not waiting for one approval")
        kind = pending[0]
        frozen = _single_interrupt(snapshot, kind)
        if frozen.get("approval_id") != approval_id:
            raise PermissionError("approval ID does not match the active interrupt")
        approver = decision_actor or (
            launch_approver() if kind == "launch_approval" else consumer_publish_approver()
        )
        approver_ctx = _ctx(
            runtime,
            actor=approver,
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="decide",
        )
        approval_service = _scenario(runtime).approvals
        current = await approval_service.get_for_decision(
            tenant_id=approver_ctx.tenant_id,
            approval_id=approval_id,
            ctx=approver_ctx,
        )
        expected_status = "approved" if decision == "approve" else "rejected"
        if current.status == "pending":
            await approval_service.decide(
                tenant_id=approver_ctx.tenant_id,
                approval_id=approval_id,
                decision=decision,
                reason=reason,
                ctx=approver_ctx,
            )
        elif current.status != expected_status or current.decision != decision:
            raise PermissionError("approval has a different terminal decision")
        value = await _graph(runtime).ainvoke(
            Command(
                resume={
                    "approval_id": approval_id,
                    "decision": decision,
                    "args_hash": frozen["args_hash"],
                    "checkpoint_id": frozen["checkpoint_id"],
                    "policy_version": frozen["policy_version"],
                }
            ),
            config=checkpoint_config(admin_ctx),
            context=admin_ctx,
        )
        return _result(thread_id, value, approval_id=approval_id, decision=decision)
    finally:
        await runtime.aclose()


async def inject_merchant_event(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    merchant_id: str,
    product_ref: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="merchant-event",
        )
        request = scenario_a_request(cast(Any, snapshot.values))
        result = await _scenario(runtime).ingest_merchant_event(
            cast(Any, snapshot.values),
            {
                "schema_version": 1,
                "event_type": "merchant.enrollment_upserted",
                "tenant_id": ctx.tenant_id,
                "adapter_id": MOCK_MERCHANT_ADAPTER,
                "source_event_id": source_event_id,
                "signature_subject": MOCK_MERCHANT_SUBJECT,
                "version": request.selection_expected_version,
                "payload": {
                    "campaign_id": request.draft.campaign_id,
                    "enrollment_id": "untrusted-caller-value",
                    "merchant_id": merchant_id,
                    "product_ref": product_ref,
                    "product_version": "v1",
                },
            },
            ctx,
        )
        return _result(
            thread_id,
            snapshot,
            event_status=result.updates.get("status"),
        )
    finally:
        await runtime.aclose()


async def close_enrollment_window(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "enrollment_window")
        request = scenario_a_request(cast(Any, snapshot.values))
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="window-close",
        )
        event = {
            "schema_version": 1,
            "event_type": "enrollment.window_closed",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_MERCHANT_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_MERCHANT_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "enrollment_window_ref": "synthetic-window-v1",
            },
        }
        value = await _graph(runtime).ainvoke(
            Command(resume=event),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, source_event_id=source_event_id)
    finally:
        await runtime.aclose()


def _confirmation_subject(snapshot: StateSnapshot, task_id: str) -> tuple[str, str]:
    values = cast(dict[str, Any], snapshot.values)
    tasks: object = values["results"]["enrollment_join"].updates.get("confirmation_tasks")
    for key, result in values["results"].items():
        if key.startswith("confirmation_decision:") and isinstance(
            result.updates.get("confirmation_tasks"), list
        ):
            tasks = result.updates["confirmation_tasks"]
    if not isinstance(tasks, list):
        raise RuntimeError("confirmation task projection is unavailable")
    for task in tasks:
        if isinstance(task, dict) and task.get("confirmation_task_id") == task_id:
            subject_type = task.get("subject_type")
            subject_id = task.get("subject_id")
            if isinstance(subject_type, str) and isinstance(subject_id, str):
                return subject_type, subject_id
    raise LookupError("confirmation task is unavailable")


async def decide_confirmation(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    confirmation_task_id: str,
    decision: Literal["confirm", "reject"],
) -> LocalWorkflowResult:
    inspection = await _runtime(config)
    try:
        _, snapshot = await _snapshot(inspection, thread_id=thread_id)
        pending = _single_interrupt(snapshot, "business_confirmation")
        if pending.get("confirmation_task_id") != confirmation_task_id:
            raise PermissionError("confirmation task does not match the active interrupt")
        subject_type, subject_id = _confirmation_subject(snapshot, confirmation_task_id)
    finally:
        await inspection.aclose()
    actor = _principal(subject_id, subject_type)
    runtime = await _runtime(
        config,
        confirmation_assignments={confirmation_task_id: subject_id},
        extra_actors=(actor,),
    )
    try:
        ctx = _ctx(
            runtime,
            actor=actor,
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="confirmation",
        )
        value = await _graph(runtime).ainvoke(
            Command(
                resume={
                    "confirmation_task_id": confirmation_task_id,
                    "decision": decision,
                }
            ),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        interruptions = _interrupts(value)
        if (
            len(interruptions) == 1
            and isinstance(interruptions[0].value, dict)
            and (interruptions[0].value.get("kind") == "workflow_handoff")
        ):
            admin_ctx = _ctx(
                runtime,
                actor=campaign_admin(),
                executor=local_cli_executor(),
                thread_id=thread_id,
                run_suffix="confirmation-handoff",
            )
            value = await _graph(runtime).ainvoke(
                Command(resume={"continue": True}),
                config=checkpoint_config(admin_ctx),
                context=admin_ctx,
            )
        return _result(
            thread_id,
            value,
            confirmation_task_id=confirmation_task_id,
            decision=decision,
        )
    finally:
        await runtime.aclose()


def _selection_values(snapshot: StateSnapshot) -> tuple[ScenarioAWorkflowRequest, str, str]:
    state = cast(dict[str, Any], snapshot.values)
    request = scenario_a_request(cast(Any, state))
    submission = state["results"]["assortment_submission"].updates["submission"]
    item_ids = state["results"]["enrollment_join"].updates["enrollment_item_ids"]
    if not isinstance(submission, dict) or not isinstance(
        submission.get("submission_version"), str
    ):
        raise RuntimeError("selection submission binding is unavailable")
    if not isinstance(item_ids, list) or not item_ids or not isinstance(item_ids[0], str):
        raise RuntimeError("selection item binding is unavailable")
    return request, submission["submission_version"], item_ids[0]


async def inject_selection_decision(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    selection_version: str,
    decision: Literal["selected", "rejected"],
    reason_code: str | None,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "selection_event")
        request, submission_version, item_id = _selection_values(snapshot)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="selection-decision",
        )
        event = {
            "schema_version": 1,
            "event_type": "selection.decision_recorded",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_SELECTION_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_SELECTION_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "submission_version": submission_version,
                "selection_version": selection_version,
                "enrollment_item_id": item_id,
                "decision": decision,
                "reason_code": reason_code,
            },
        }
        applied = await _scenario(runtime).ingest_selection_decision(
            request, cast(Any, snapshot.values), event, ctx
        )
        graph = _graph(runtime)
        await graph.aupdate_state(
            checkpoint_config(ctx),
            {
                "results": {
                    f"selection_decision:{item_id}": applied,
                }
            },
            as_node="prepare_selection_wait",
        )
        value = await graph.ainvoke(
            None,
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(
            thread_id,
            value,
            selection_version=applied.updates.get("selection_version"),
            decision=decision,
        )
    finally:
        await runtime.aclose()


async def complete_selection(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    selection_version: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "selection_event")
        request, submission_version, _ = _selection_values(snapshot)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="selection-complete",
        )
        event = {
            "schema_version": 1,
            "event_type": "selection.completed",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_SELECTION_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_SELECTION_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "submission_version": submission_version,
                "selection_version": selection_version,
            },
        }
        value = await _graph(runtime).ainvoke(
            Command(resume=event),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, selection_version=selection_version)
    finally:
        await runtime.aclose()


def workflow_database_paths(config: ResolvedRuntimeConfig) -> tuple[Path, Path]:
    """Return paths used by tests to independently inspect fixture persistence."""

    return config.data_paths.platform_db, config.data_paths.business_db
