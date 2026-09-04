"""Pure terminal presentation for the Scenario A workflow.

This module deliberately depends only on immutable value models. It must not load
repositories, runtime services, checkpoints, or LangGraph implementation objects.
"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from oria.agent.models import CampaignProposal
from oria.core.types import ValueModel

TerminalOutcome = Literal["completed", "rejected", "failed", "reconciliation_required"]
StepStatus = Literal["completed", "current", "pending", "rejected", "failed", "reconciliation"]


class RuleSummaryItem(ValueModel):
    category: str = Field(min_length=1)
    key_value: str = Field(min_length=1)
    effective_time: str = Field(min_length=1)
    source_version: str = Field(min_length=1)


class MerchantMatch(ValueModel):
    merchant_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    hard_eligibility: Literal["合格"] = "合格"
    llm_rank: int | None = Field(default=None, ge=1)
    recommendation_reason: str = Field(min_length=1)


class MerchantMatches(ValueModel):
    matched_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    items: tuple[MerchantMatch, ...] = ()


class MerchantExclusionCount(ValueModel):
    reason: str = Field(min_length=1)
    count: int = Field(gt=0)


class ApprovalSummary(ValueModel):
    kind: Literal["launch_approval", "consumer_publish_approval"]
    approval_id: str = Field(min_length=1)
    status: Literal["pending", "approved", "rejected"]
    description: str = Field(min_length=1)


class ConfirmationProgress(ValueModel):
    current_level: int = Field(ge=1)
    total_levels: int = Field(ge=1)
    current_role: str = Field(min_length=1)
    next_role: str | None = None


class SelectionSummary(ValueModel):
    submission_version: str | None = None
    submitted_count: int = Field(default=0, ge=0)
    received_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    selected_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    selected_products: tuple[str, ...] = ()
    coupon_linked_count: int = Field(default=0, ge=0)
    placement_scope: str | None = None


class EnrollmentItemDetail(ValueModel):
    merchant_id: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    product_version: str | None = None
    sources: tuple[str, ...] = ()
    status: str = Field(min_length=1)


class CouponBatchSummary(ValueModel):
    coupon_batch_id: str = Field(min_length=1)
    face_values: tuple[str, ...] = ()
    quantity: int | None = Field(default=None, ge=0)
    quantity_note: str | None = None
    budget_cap: str | None = None
    currency: str | None = None
    status: str = Field(min_length=1)


class SelectionDecisionDetail(ValueModel):
    merchant_id: str | None = None
    product_ref: str = Field(min_length=1)
    product_version: str | None = None
    decision: Literal["selected", "rejected"]
    reason: str = Field(min_length=1)


class PlacementSummary(ValueModel):
    channel: str = Field(min_length=1)
    region: str = Field(min_length=1)
    content_example: str = Field(min_length=1)
    selected_products: tuple[str, ...] = ()
    status: str = Field(min_length=1)


class NotificationMessage(ValueModel):
    merchant_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str = Field(min_length=1)


class WorkflowViewModel(ValueModel):
    """Stable, secret-free projection consumed by human renderers."""

    thread_id: str = Field(min_length=1)
    flow_name: str = Field(min_length=1)
    stage_index: int = Field(ge=1)
    stage_total: int = Field(ge=1)
    current_stage: str = Field(min_length=1)
    completed_steps: tuple[str, ...] = ()
    pending_action: str = Field(min_length=1)
    next_command: str | None = None
    rule_summary: tuple[RuleSummaryItem, ...] = ()
    merchant_matches: MerchantMatches
    merchant_exclusion_summary: tuple[MerchantExclusionCount, ...] = ()
    approval_summary: ApprovalSummary | None = None
    confirmation_progress: ConfirmationProgress | None = None
    selection_summary: SelectionSummary = SelectionSummary()
    enrollment_items: tuple[EnrollmentItemDetail, ...] = ()
    coupon_batch: CouponBatchSummary | None = None
    selection_decisions: tuple[SelectionDecisionDetail, ...] = ()
    placement: PlacementSummary | None = None
    notification_messages: tuple[NotificationMessage, ...] = ()
    terminal_outcome: TerminalOutcome | None = None


_ROLE_LABELS = {
    "merchant": "商家",
    "sales": "销售",
    "sales_manager": "销售经理",
}


def proposal_rule_summary(
    proposal: CampaignProposal | None, effective_at: str
) -> tuple[RuleSummaryItem, ...]:
    """Project the six public rule categories without restricted source fields."""

    if proposal is None or proposal.rules is None:
        return ()
    rules = proposal.rules
    versions: dict[str, set[str]] = {key: set() for key in type(rules).model_fields}
    for path, citation in proposal.field_evidence.items():
        category = path.split(".", maxsplit=1)[0]
        if category in versions:
            versions[category].add(citation.document_version)

    def version(category: str) -> str:
        found = versions[category]
        return ", ".join(sorted(found)) if found else "—"

    benefit = rules.benefit_policy
    confirmation_roles = " → ".join(
        _ROLE_LABELS.get(role, role) for role in rules.confirmation_policy.ordered_steps
    )
    return (
        RuleSummaryItem(
            category="基础信息",
            key_value=(
                f"模板 {rules.basic.template_ref}; 类型 {rules.basic.campaign_type}; "
                f"商品范围 {', '.join(rules.basic.product_scope)}"
            ),
            effective_time=rules.basic.campaign_window,
            source_version=version("basic"),
        ),
        RuleSummaryItem(
            category="招商范围",
            key_value=(
                f"类目 {', '.join(rules.recruitment_scope.categories)}; "
                f"城市 {', '.join(rules.recruitment_scope.cities)}; "
                f"报名系统 {', '.join(rules.recruitment_scope.enrollment_systems)}"
            ),
            effective_time=effective_at,
            source_version=version("recruitment_scope"),
        ),
        RuleSummaryItem(
            category="报名规则",
            key_value=(
                f"模式 {rules.enrollment_policy.mode}; "
                f"圈品策略 {rules.enrollment_policy.product_circle_policy_ref}"
            ),
            effective_time=rules.basic.enrollment_window,
            source_version=version("enrollment_policy"),
        ),
        RuleSummaryItem(
            category="优惠档位",
            key_value=(
                f"{', '.join(benefit.tiers)}; 预算上限 {benefit.budget_cap} {benefit.currency}"
            ),
            effective_time=rules.basic.campaign_window,
            source_version=version("benefit_policy"),
        ),
        RuleSummaryItem(
            category="确认规则",
            key_value=(f"{confirmation_roles}; 超时 {rules.confirmation_policy.timeout_action}"),
            effective_time=effective_at,
            source_version=version("confirmation_policy"),
        ),
        RuleSummaryItem(
            category="商家端素材",
            key_value=(
                f"标题 {rules.merchant_material.title}; 标签 "
                f"{', '.join(rules.merchant_material.tags)}"
            ),
            effective_time=rules.basic.campaign_window,
            source_version=version("merchant_material"),
        ),
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


def render_workflow(view: WorkflowViewModel) -> str:
    """Render natural-language status and deterministic terminal tables."""

    lines = [
        f"{view.flow_name} · 第 {view.stage_index}/{view.stage_total} 阶段",
        f"线程: {view.thread_id}",
        f"当前阶段: {view.current_stage}",
        _outcome_sentence(view),
    ]
    if view.next_command is not None:
        lines.append(f"下一步命令: {view.next_command}")

    lines.extend(["", "规则摘要", _rule_table(view), "", "商家候选"])
    lines.append(
        f"命中 {view.merchant_matches.matched_count} / "
        f"评估 {view.merchant_matches.evaluated_count} 家"
    )
    lines.append(_merchant_table(view))
    if view.merchant_exclusion_summary:
        summary = "; ".join(
            f"{item.reason} {item.count}" for item in view.merchant_exclusion_summary
        )
        lines.extend(["", "未命中原因汇总", summary])

    if view.enrollment_items:
        lines.extend(["", "报名商品", _enrollment_table(view)])

    if view.coupon_batch is not None:
        lines.extend(["", "券批次", _coupon_batch_table(view.coupon_batch)])

    selection = view.selection_summary
    if selection.submission_version is not None:
        lines.extend(
            [
                "",
                "选品进度",
                (
                    f"批次 {selection.submission_version}: 已提交 {selection.submitted_count}, "
                    f"已收 {selection.received_count}, 待收 {selection.pending_count}; "
                    f"入选 {selection.selected_count}, 未入选 {selection.rejected_count}"
                ),
            ]
        )
        if selection.selected_products:
            lines.append(f"入选商品: {'、'.join(selection.selected_products)}")
        if selection.coupon_linked_count:
            lines.append(f"券关联: {selection.coupon_linked_count} 个报名商品已关联")
        if selection.placement_scope:
            lines.append(f"投放范围: {selection.placement_scope}")

    if view.selection_decisions:
        lines.extend(["", "选品商品明细", _selection_decision_table(view)])

    if view.placement is not None:
        lines.extend(["", "C 端投放", _placement_table(view.placement)])

    if view.notification_messages:
        lines.extend(["", "商家通知文案", _notification_table(view)])

    lines.extend(["", "流程进度", _workflow_table(view)])
    return "\n".join(lines)


def _outcome_sentence(view: WorkflowViewModel) -> str:
    if view.terminal_outcome == "completed":
        return "流程已完成: C 端投放与商家通知已闭环。"
    if view.terminal_outcome == "rejected":
        return "审批或业务确认已拒绝, 流程已终止, 不会继续执行后续投放。"
    if view.terminal_outcome == "failed":
        return "流程执行失败, 已停止推进; 请根据当前阶段排查后重新发起。"
    if view.terminal_outcome == "reconciliation_required":
        return "外部副作用状态未确认, 流程已进入人工对账, 禁止盲目重试。"
    return view.pending_action


def _rule_table(view: WorkflowViewModel) -> str:
    rows = [
        (item.category, item.key_value, item.effective_time, item.source_version)
        for item in view.rule_summary
    ] or [("—", "尚未生成规则摘要", "—", "—")]
    return _table(
        ("类别", "关键值", "生效时间", "来源版本"),
        rows,
        minimums=(10, 16, 14, 10),
        long_text_columns=(1,),
    )


def _merchant_table(view: WorkflowViewModel) -> str:
    rows: list[tuple[str, str, str, str]] = [
        (
            f"{item.display_name} ({item.merchant_id})",
            item.hard_eligibility,
            str(item.llm_rank) if item.llm_rank is not None else "—",
            item.recommendation_reason,
        )
        for item in view.merchant_matches.items
    ] or [("—", "—", "—", "尚无合格候选")]
    return _table(
        ("商家", "硬资格", "LLM 排名", "推荐理由"),
        rows,
        minimums=(16, 6, 8, 16),
        long_text_columns=(3,),
    )


def _enrollment_table(view: WorkflowViewModel) -> str:
    rows = [
        (
            item.merchant_id,
            (
                f"{item.product_ref} ({item.product_version})"
                if item.product_version is not None
                else item.product_ref
            ),
            " + ".join(_source_label(source) for source in item.sources) or "未标注",
            _business_status_label(item.status),
        )
        for item in view.enrollment_items
    ]
    return _table(
        ("商家", "商品", "报名来源", "状态"),
        rows,
        minimums=(14, 20, 10, 8),
        long_text_columns=(1,),
    )


def _coupon_batch_table(batch: CouponBatchSummary) -> str:
    quantity = (
        str(batch.quantity) if batch.quantity is not None else batch.quantity_note or "未配置"
    )
    budget = (
        f"{batch.budget_cap} {batch.currency or ''}".strip()
        if batch.budget_cap is not None
        else "未配置"
    )
    return _table(
        ("批次", "面额 / 档位", "数量", "预算上限", "状态"),
        [
            (
                batch.coupon_batch_id,
                "; ".join(batch.face_values) or "未配置",
                quantity,
                budget,
                _business_status_label(batch.status),
            )
        ],
        minimums=(16, 18, 10, 12, 8),
        long_text_columns=(1,),
    )


def _selection_decision_table(view: WorkflowViewModel) -> str:
    rows = [
        (
            item.merchant_id or "—",
            (
                f"{item.product_ref} ({item.product_version})"
                if item.product_version is not None
                else item.product_ref
            ),
            "入选" if item.decision == "selected" else "未入选",
            _selection_reason_label(item.reason),
        )
        for item in view.selection_decisions
    ]
    return _table(
        ("商家", "商品", "结果", "原因"),
        rows,
        minimums=(14, 20, 8, 16),
        long_text_columns=(3,),
    )


def _placement_table(placement: PlacementSummary) -> str:
    return _table(
        ("渠道", "区域", "状态", "投放文案示例"),
        [
            (
                placement.channel,
                placement.region,
                _business_status_label(placement.status),
                placement.content_example,
            )
        ],
        minimums=(12, 12, 8, 20),
        long_text_columns=(3,),
    )


def _notification_table(view: WorkflowViewModel) -> str:
    rows = [
        (
            item.merchant_id,
            item.channel,
            _business_status_label(item.status),
            item.message,
        )
        for item in view.notification_messages
    ]
    return _table(
        ("商家", "渠道", "状态", "通知文案"),
        rows,
        minimums=(14, 12, 8, 20),
        long_text_columns=(3,),
    )


def _source_label(source: str) -> str:
    return {"merchant": "商家自主报名", "auto": "系统自动圈品"}.get(source, source)


def _business_status_label(status: str) -> str:
    return {
        "draft": "草稿",
        "materializing": "物化中",
        "ready": "已就绪",
        "pending_confirmation": "待确认",
        "confirmed": "已确认",
        "rejected": "已拒绝",
        "pending_approval": "待审批",
        "published": "已投放",
        "sent": "已发送",
        "dead_letter": "死信",
        "failed": "失败",
        "unknown": "待对账",
    }.get(status, status)


def _selection_reason_label(reason: str) -> str:
    return {
        "selected_by_assortment": "通过招后选品",
        "selection_reason_unavailable": "外部选品原因未进入展示投影",
    }.get(reason, reason)


def _workflow_table(view: WorkflowViewModel) -> str:
    completed = set(view.completed_steps)
    rows: list[tuple[str, str, str, str]] = []
    for index, step in enumerate(_STAGES[: view.stage_total], start=1):
        if step in completed:
            status: StepStatus = "completed"
            artifact = "已生成"
            action = "—"
        elif index == view.stage_index:
            status = _current_status(view)
            artifact = _current_artifact(view)
            action = view.pending_action
        else:
            status = "pending"
            artifact = "—"
            action = "等待前序步骤"
        rows.append((f"{index}. {step}", _status_label(status), artifact, action))
    return _table(
        ("步骤", "状态", "产物", "下一动作"),
        rows,
        minimums=(18, 6, 12, 16),
        long_text_columns=(3,),
    )


def _current_status(view: WorkflowViewModel) -> StepStatus:
    if view.terminal_outcome == "rejected":
        return "rejected"
    if view.terminal_outcome == "failed":
        return "failed"
    if view.terminal_outcome == "reconciliation_required":
        return "reconciliation"
    if view.terminal_outcome == "completed":
        return "completed"
    return "current"


def _current_artifact(view: WorkflowViewModel) -> str:
    if view.approval_summary is not None:
        return f"审批 {view.approval_summary.approval_id}"
    if view.confirmation_progress is not None:
        progress = view.confirmation_progress
        return f"第 {progress.current_level}/{progress.total_levels} 级"
    if view.selection_summary.submission_version is not None:
        return f"选品批次 {view.selection_summary.submission_version}"
    if view.terminal_outcome is not None:
        return "终态已记录"
    return "处理中"


def _status_label(status: StepStatus) -> str:
    return {
        "completed": "已完成",
        "current": "进行中",
        "pending": "待开始",
        "rejected": "已拒绝",
        "failed": "失败",
        "reconciliation": "待对账",
    }[status]


def _table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    minimums: Sequence[int],
    long_text_columns: Sequence[int],
) -> str:
    normalized = [tuple(str(cell) for cell in row) for row in rows]
    widths = _column_widths(headers, normalized, minimums, long_text_columns)
    top = "┌" + "┬".join("─" * (width + 2) for width in widths) + "┐"
    middle = "├" + "┼".join("─" * (width + 2) for width in widths) + "┤"
    bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"

    def line(cells: Sequence[str]) -> list[str]:
        wrapped = [_wrap(cell, widths[index]) for index, cell in enumerate(cells)]
        height = max(len(cell_lines) for cell_lines in wrapped)
        return [
            "│ "
            + " │ ".join(
                _pad(cell_lines[line_index] if line_index < len(cell_lines) else "", widths[index])
                for index, cell_lines in enumerate(wrapped)
            )
            + " │"
            for line_index in range(height)
        ]

    body = line(headers)
    body.append(middle)
    for row in normalized:
        body.extend(line(row))
    return "\n".join([top, *body, bottom])


def _column_widths(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    minimums: Sequence[int],
    long_text_columns: Sequence[int],
) -> list[int]:
    column_count = len(headers)
    if len(minimums) != column_count:
        raise ValueError("table minimum widths must match the column count")
    available = max(_terminal_columns() - (3 * column_count + 1), 2 * column_count)
    natural = [
        max(_display_width(headers[index]), *(_display_width(row[index]) for row in rows))
        for index in range(column_count)
    ]
    widths = [max(width, 2) for width in minimums]
    for index in long_text_columns:
        widths[index] = max(widths[index], 60)

    excess = max(sum(widths) - available, 0)
    shrink_order = [
        *long_text_columns,
        *(index for index in range(column_count) if index not in long_text_columns),
    ]
    for index in shrink_order:
        floor = max(minimums[index], 2) if index in long_text_columns else 2
        reduction = min(max(widths[index] - floor, 0), excess)
        widths[index] -= reduction
        excess -= reduction
        if excess == 0:
            break
    if excess:
        for index in shrink_order:
            reduction = min(max(widths[index] - 2, 0), excess)
            widths[index] -= reduction
            excess -= reduction
            if excess == 0:
                break

    remaining = max(available - sum(widths), 0)
    for index in shrink_order:
        addition = min(max(natural[index] - widths[index], 0), remaining)
        widths[index] += addition
        remaining -= addition
    return widths


def _terminal_columns() -> int:
    try:
        if not sys.stdout.isatty():
            return 160
    except (AttributeError, OSError):
        return 160
    return max(shutil.get_terminal_size(fallback=(160, 24)).columns, 21)


def _wrap(value: str, width: int) -> tuple[str, ...]:
    lines: list[str] = []
    current = ""
    current_width = 0
    for character in value:
        if character == "\n":
            lines.append(current)
            current = ""
            current_width = 0
            continue
        character_width = _display_width(character)
        if current and current_width + character_width > width:
            lines.append(current)
            current = ""
            current_width = 0
        current += character
        current_width += character_width
    lines.append(current)
    return tuple(lines)


def _display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1 for character in value
    )


def _pad(value: str, width: int) -> str:
    return value + " " * (width - _display_width(value))
