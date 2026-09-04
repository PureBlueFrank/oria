"""Human workflow projection and terminal renderer semantics."""

from typing import cast

import pytest

from oria.core.types import JsonValue, NodeError, NodeResult
from oria.orchestrator.local_executor import (
    LocalWorkflowResult,
    default_request,
    workflow_view_from_state,
)
from oria.orchestrator.scenario_a import initial_scenario_a_state
from oria.presentation.workflow import (
    CouponBatchSummary,
    EnrollmentItemDetail,
    MerchantExclusionCount,
    MerchantMatch,
    MerchantMatches,
    NotificationMessage,
    PlacementSummary,
    RuleSummaryItem,
    SelectionDecisionDetail,
    SelectionSummary,
    WorkflowViewModel,
    _column_widths,
    _display_width,
    _wrap,
    render_workflow,
)

pytestmark = pytest.mark.unit


def _state(results: dict[str, NodeResult]) -> dict[str, object]:
    request = default_request(campaign_id="campaign-view", user_request="测试展示")
    state = initial_scenario_a_state(
        request,
        meta={
            "tenant_id": "local-community",
            "session_id": "session-view",
            "thread_id": "thread-view",
            "run_id": "run-view",
            "job_id": None,
            "requester_subject_id": "local-campaign-admin",
        },
    )
    return {**state, "results": results}


def _interrupt(kind: str, **values: JsonValue) -> tuple[dict[str, JsonValue], ...]:
    return (cast(dict[str, JsonValue], {"kind": kind, **values}),)


@pytest.mark.parametrize(
    ("kind", "values", "expected", "command"),
    [
        (
            "launch_approval",
            {"approval_id": "approval-launch"},
            "等待招商发布审批",
            "oria approval approve",
        ),
        (
            "enrollment_window",
            {"wait_id": "wait-window"},
            "等待报名",
            "oria mock window-close",
        ),
        (
            "selection_event",
            {"wait_id": "wait-selection"},
            "等待剩余选品结果",
            "oria mock selection-complete",
        ),
        (
            "consumer_publish_approval",
            {"approval_id": "approval-consumer"},
            "等待 C 端发布审批",
            "oria approval approve",
        ),
    ],
)
def test_interrupt_kinds_have_natural_language_and_next_command(
    kind: str,
    values: dict[str, JsonValue],
    expected: str,
    command: str,
) -> None:
    state = _state({"draft": NodeResult(status="completed")})
    view = workflow_view_from_state("thread-view", state, _interrupt(kind, **values), {})
    rendered = render_workflow(view)

    assert expected in rendered
    assert command in rendered
    assert "规则摘要" in rendered
    assert "商家候选" in rendered
    assert "流程进度" in rendered


def test_business_confirmation_shows_level_roles_and_next_role() -> None:
    tasks = [
        {
            "confirmation_task_id": "task-merchant",
            "sequence": 1,
            "subject_type": "merchant",
            "status": "pending",
        },
        {
            "confirmation_task_id": "task-sales",
            "sequence": 2,
            "subject_type": "sales",
            "status": "waiting",
        },
    ]
    state = _state(
        {
            "enrollment_join": NodeResult(
                status="completed",
                updates={"confirmation_tasks": cast(JsonValue, tasks)},
            )
        }
    )
    view = workflow_view_from_state(
        "thread-view",
        state,
        _interrupt(
            "business_confirmation",
            confirmation_task_id="task-merchant",
            wait_id="wait-confirmation",
        ),
        {},
    )
    rendered = render_workflow(view)

    assert "当前第 1/2 级" in rendered
    assert "由商家确认, 下一位为销售" in rendered
    assert "oria workflow resume" in rendered


@pytest.mark.parametrize(
    ("results", "outcome", "expected"),
    [
        (
            {
                "launch": NodeResult(
                    status="failed",
                    error=NodeError(
                        code="launch_rejected",
                        safe_message="Workflow approval was rejected",
                    ),
                )
            },
            "rejected",
            "已拒绝",
        ),
        (
            {
                "draft": NodeResult(
                    status="failed",
                    error=NodeError(code="draft_failed", safe_message="draft failed"),
                )
            },
            "failed",
            "流程执行失败",
        ),
        (
            {
                "launch": NodeResult(
                    status="waiting",
                    updates={"saga": {"status": "reconciliation_required"}},
                )
            },
            "reconciliation_required",
            "人工对账",
        ),
        (
            {"merchant_notifications": NodeResult(status="completed")},
            "completed",
            "流程已完成",
        ),
    ],
)
def test_terminal_outcomes_are_not_inferred_only_from_interrupt_absence(
    results: dict[str, NodeResult],
    outcome: str,
    expected: str,
) -> None:
    view = workflow_view_from_state("thread-view", _state(results), (), {})
    rendered = render_workflow(view)

    assert view.terminal_outcome == outcome
    assert expected in rendered
    assert "流程进度" in rendered


def test_renderer_contains_rule_merchant_and_workflow_table_rows() -> None:
    view = WorkflowViewModel(
        thread_id="thread-table",
        flow_name="招商活动自动化",
        stage_index=7,
        stage_total=10,
        current_stage="提交并等待招后选品",
        completed_steps=("生成规则与活动草案",),
        pending_action="选品批次已提交, 正在等待剩余选品结果。",
        next_command="oria mock selection-complete --thread-id thread-table",
        rule_summary=(
            RuleSummaryItem(
                category="基础信息",
                key_value="华东餐饮暑期活动",
                effective_time="2026-07-01/2026-08-31",
                source_version="1.0.0",
            ),
        ),
        merchant_matches=MerchantMatches(
            matched_count=1,
            evaluated_count=3,
            items=(
                MerchantMatch(
                    merchant_id="demo-m001",
                    display_name="虚构食坊一号",
                    llm_rank=1,
                    recommendation_reason="活动匹配度高",
                ),
            ),
        ),
        merchant_exclusion_summary=(
            MerchantExclusionCount(reason="城市不符", count=1),
            MerchantExclusionCount(reason="名单策略未通过", count=1),
        ),
        selection_summary=SelectionSummary(
            submission_version="submission-v1",
            submitted_count=2,
            received_count=1,
            pending_count=1,
            selected_count=1,
            selected_products=("synthetic-product-1",),
            coupon_linked_count=2,
            placement_scope="region=east-china",
        ),
        enrollment_items=(
            EnrollmentItemDetail(
                merchant_id="demo-m001",
                product_ref="synthetic-product-1",
                product_version="v1",
                sources=("auto", "merchant"),
                status="confirmed",
            ),
        ),
        coupon_batch=CouponBatchSummary(
            coupon_batch_id="coupon-1",
            face_values=("base: 10 CNY",),
            quantity_note="未单独配置固定张数",
            budget_cap="100000.00",
            currency="CNY",
            status="ready",
        ),
        selection_decisions=(
            SelectionDecisionDetail(
                merchant_id="demo-m001",
                product_ref="synthetic-product-1",
                product_version="v1",
                decision="selected",
                reason="selected_by_assortment",
            ),
        ),
        placement=PlacementSummary(
            channel="home-feed",
            region="east-china",
            content_example="夏季活动: 1 个入选商品已配置优惠。",
            selected_products=("synthetic-product-1",),
            status="published",
        ),
        notification_messages=(
            NotificationMessage(
                merchant_id="demo-m001",
                channel="mock-im",
                status="sent",
                message="活动选品结果: 入选商品 synthetic-product-1。",
            ),
        ),
    )
    rendered = render_workflow(view)

    assert "│ 类别" in rendered
    assert "基础信息" in rendered
    assert "│ 商家" in rendered
    assert "虚构食坊一号" in rendered
    assert "│ 步骤" in rendered
    assert "提交并等待招后选品" in rendered
    assert "城市不符 1; 名单策略未通过 1" in rendered
    assert "已提交 2, 已收 1, 待收 1" in rendered
    assert "报名商品" in rendered
    assert "系统自动圈品 + 商家自主报名" in rendered
    assert "券批次" in rendered
    assert "base: 10 CNY" in rendered
    assert "选品商品明细" in rendered
    assert "通过招后选品" in rendered
    assert "C 端投放" in rendered
    assert "投放文案示例" in rendered
    assert "商家通知文案" in rendered


def test_renderer_omits_optional_business_detail_sections_when_empty() -> None:
    view = WorkflowViewModel(
        thread_id="thread-empty",
        flow_name="招商活动自动化",
        stage_index=1,
        stage_total=10,
        current_stage="生成规则与活动草案",
        pending_action="处理中",
        merchant_matches=MerchantMatches(matched_count=0, evaluated_count=0),
    )

    rendered = render_workflow(view)

    assert "\n报名商品\n" not in rendered
    assert "\n券批次\n" not in rendered
    assert "\n选品商品明细\n" not in rendered
    assert "\nC 端投放\n" not in rendered
    assert "\n商家通知文案\n" not in rendered


def test_workflow_detail_fields_default_to_empty() -> None:
    view = WorkflowViewModel(
        thread_id="thread-defaults",
        flow_name="招商活动自动化",
        stage_index=1,
        stage_total=10,
        current_stage="生成规则与活动草案",
        pending_action="处理中",
        merchant_matches=MerchantMatches(matched_count=0, evaluated_count=0),
    )

    assert view.enrollment_items == ()
    assert view.coupon_batch is None
    assert view.selection_decisions == ()
    assert view.placement is None
    assert view.notification_messages == ()


def test_state_projection_reads_optional_business_detail_updates() -> None:
    state = _state(
        {
            "enrollment_join": NodeResult(
                status="completed",
                updates={
                    "enrollment_items": cast(
                        JsonValue,
                        [
                            {
                                "merchant_id": "demo-m001",
                                "product_ref": "synthetic-product-1",
                                "product_version": "v1",
                                "sources": ["auto", "merchant"],
                                "status": "confirmed",
                            }
                        ],
                    )
                },
            ),
            "launch": NodeResult(
                status="completed",
                updates={
                    "coupon_batch": cast(
                        JsonValue,
                        {
                            "coupon_batch_id": "coupon-1",
                            "face_values": ["base: 10 CNY"],
                            "quantity": None,
                            "quantity_note": "未单独配置固定张数",
                            "budget_cap": "100000.00",
                            "currency": "CNY",
                            "status": "ready",
                        },
                    )
                },
            ),
            "consumer_publish_approval": NodeResult(
                status="waiting",
                updates={
                    "selection_decisions": cast(
                        JsonValue,
                        [
                            {
                                "merchant_id": "demo-m001",
                                "product_ref": "synthetic-product-1",
                                "product_version": "v1",
                                "decision": "selected",
                                "reason": "selected_by_assortment",
                            }
                        ],
                    ),
                    "placement_display": cast(
                        JsonValue,
                        {
                            "channel": "home-feed",
                            "region": "east-china",
                            "content_example": "夏季活动: 1 个入选商品已配置优惠。",
                            "selected_products": ["synthetic-product-1"],
                            "status": "pending_approval",
                        },
                    ),
                },
            ),
            "merchant_notifications": NodeResult(
                status="completed",
                updates={
                    "notification_messages": cast(
                        JsonValue,
                        [
                            {
                                "merchant_id": "demo-m001",
                                "channel": "mock-im",
                                "status": "sent",
                                "message": "活动选品结果: 入选商品 synthetic-product-1。",
                            }
                        ],
                    )
                },
            ),
        }
    )

    view = workflow_view_from_state("thread-view", state, (), {})

    assert view.enrollment_items[0].merchant_id == "demo-m001"
    assert view.enrollment_items[0].sources == ("auto", "merchant")
    assert view.coupon_batch is not None
    assert view.coupon_batch.status == "ready"
    assert view.selection_decisions[0].decision == "selected"
    assert view.placement is not None
    assert view.placement.content_example.startswith("夏季活动")
    assert view.notification_messages[0].message.startswith("活动选品结果")


def test_state_projection_ignores_missing_optional_business_detail_updates() -> None:
    view = workflow_view_from_state(
        "thread-view",
        _state({"draft": NodeResult(status="completed")}),
        _interrupt("launch_approval", approval_id="approval-launch"),
        {},
    )

    assert view.enrollment_items == ()
    assert view.coupon_batch is None
    assert view.selection_decisions == ()
    assert view.placement is None
    assert view.notification_messages == ()


def test_renderer_wraps_long_cells_without_ellipsis_or_exceeding_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("oria.presentation.workflow._terminal_columns", lambda: 80)
    long_reason = "满足规则快照中的全部确定性硬资格且可通过当前报名系统完成活动报名"
    view = WorkflowViewModel(
        thread_id="thread-wrap",
        flow_name="招商活动自动化",
        stage_index=2,
        stage_total=10,
        current_stage="招商发布审批",
        completed_steps=("生成规则与活动草案",),
        pending_action="规则、候选范围及活动/券草案已冻结, 等待招商发布审批。",
        next_command=("oria approval approve --thread-id thread-wrap --approval-id approval-wrap"),
        merchant_matches=MerchantMatches(
            matched_count=1,
            evaluated_count=1,
            items=(
                MerchantMatch(
                    merchant_id="demo-m001",
                    display_name="虚构食坊一号",
                    llm_rank=1,
                    recommendation_reason=long_reason,
                ),
            ),
        ),
    )

    rendered = render_workflow(view)

    assert "…" not in rendered
    assert "完成活动报名" in rendered
    table_lines = [line for line in rendered.splitlines() if line.startswith(("┌", "├", "└", "│"))]
    assert table_lines
    assert all(_display_width(line) <= 80 for line in table_lines)
    assert "".join(_wrap(long_reason, 16)) == long_reason


def test_non_tty_fallback_reserves_wide_long_text_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("oria.presentation.workflow._terminal_columns", lambda: 160)
    widths = _column_widths(
        ("类别", "关键值", "生效时间", "来源版本"),
        (("基础信息", "短值", "2026-07-10", "1.0.0"),),
        (10, 16, 14, 10),
        (1,),
    )

    assert widths[1] >= 60


def test_local_result_view_does_not_change_json_contract() -> None:
    view = workflow_view_from_state(
        "thread-view",
        _state({"merchant_notifications": NodeResult(status="completed")}),
        (),
        {},
    )
    result = LocalWorkflowResult(
        thread_id="thread-view",
        status="completed",
        detail={"decision": "reject"},
        view=view,
    )

    assert result.model_dump(mode="json") == {
        "ok": True,
        "thread_id": "thread-view",
        "status": "completed",
        "interrupts": [],
        "detail": {"decision": "reject"},
    }
