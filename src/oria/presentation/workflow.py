"""Pure terminal presentation for the Scenario A workflow.

This module deliberately depends only on immutable value models. It must not load
repositories, runtime services, checkpoints, or LangGraph implementation objects.
"""

from __future__ import annotations

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
    """Render natural-language status and three deterministic terminal tables."""

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
    return _table(("类别", "关键值", "生效时间", "来源版本"), rows, (14, 36, 25, 14))


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
    return _table(("商家", "硬资格", "LLM 排名", "推荐理由"), rows, (28, 8, 10, 38))


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
    return _table(("步骤", "状态", "产物", "下一动作"), rows, (28, 10, 22, 38))


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
    maximums: Sequence[int],
) -> str:
    normalized = [
        tuple(_truncate(str(cell), maximums[index]) for index, cell in enumerate(row))
        for row in rows
    ]
    widths = [
        min(
            maximums[index],
            max(
                _display_width(headers[index]), *(_display_width(row[index]) for row in normalized)
            ),
        )
        for index in range(len(headers))
    ]
    top = "┌" + "┬".join("─" * (width + 2) for width in widths) + "┐"
    middle = "├" + "┼".join("─" * (width + 2) for width in widths) + "┤"
    bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"

    def line(cells: Sequence[str]) -> str:
        return (
            "│ " + " │ ".join(_pad(cell, widths[index]) for index, cell in enumerate(cells)) + " │"
        )

    return "\n".join([top, line(headers), middle, *(line(row) for row in normalized), bottom])


def _display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1 for character in value
    )


def _truncate(value: str, maximum: int) -> str:
    if _display_width(value) <= maximum:
        return value
    result = ""
    for character in value:
        if _display_width(result + character + "…") > maximum:
            break
        result += character
    return result + "…"


def _pad(value: str, width: int) -> str:
    return value + " " * (width - _display_width(value))
