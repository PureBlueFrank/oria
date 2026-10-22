"""Explicit synthetic walkthrough over the existing local Workflow and Agent.

The remote participant controls a demonstration, not business identities. Local
fixture principals, approvals, events and adapters remain owned by local_executor.
This is deliberately separate from the production chat/Job assembly.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from oria.attribution_demo import run_attribution_ask
from oria.chat.router import IntentRouter, LaunchCampaign, RouterPrompt
from oria.config import resolve_runtime_config
from oria.core.types import ValueModel
from oria.orchestrator.local_executor import (
    LocalWorkflowResult,
    close_enrollment_window,
    complete_selection,
    decide_confirmation,
    decide_local_approval,
    inject_merchant_event,
    inject_selection_decision,
    inspect_local_workflow,
    start_local_workflow,
)
from oria.presentation.im import IMSection, IMViewModel

BOUNDARY = "真实飞书通道 · 合成数据 · Mock 模型/业务系统 · 演示角色"


class DemoInput(ValueModel):
    """Allowlisted, canonical input; never an original message or arbitrary tool args."""

    kind: Literal[
        "help",
        "slots",
        "start",
        "status",
        "rules",
        "merchants",
        "approve",
        "confirm",
        "enroll",
        "close",
        "selection",
        "attribution",
        "conflict",
        "insufficient",
        "page",
        "unknown",
    ]
    slots: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1, le=20)


def demo_input(text: str) -> DemoInput:
    """Only preserve known fixture slots and explicit demonstration commands."""
    if text in {f"第{i}页" for i in range(1, 21)}:
        return DemoInput(kind="page", page=int(text[1:-1]))
    aliases = {
        "帮助": "help",
        "/help": "help",
        "确认发起演示": "start",
        "状态": "status",
        "恢复演示": "status",
        "规则": "rules",
        "商家": "merchants",
        "演示批准": "approve",
        "演示确认": "confirm",
        "模拟商家报名": "enroll",
        "模拟报名结束": "close",
        "模拟选品完成": "selection",
        "归因示例": "attribution",
        "证据冲突示例": "conflict",
        "证据不足示例": "insufficient",
    }
    if text in aliases:
        return DemoInput.model_validate({"kind": aliases[text]})
    # The frozen fixture supports this one published scenario. Do not imply that
    # arbitrary natural-language constraints changed its underlying business data.
    if text in {"发起招商活动", "餐饮", "华东", "混合报名", "10家"}:
        return DemoInput(kind="slots", slots=text)
    return DemoInput(kind="unknown")


class _State(ValueModel):
    version: Literal[1] = 1
    thread_id: str
    campaign_id: str
    inputs: tuple[str, ...] = ()
    prepared: LaunchCampaign | None = None
    started: bool = False


class GuidedDemo:
    """Single isolated fixture session; asynchronous effects use existing services."""

    def __init__(self, root: Path, manifest: Path) -> None:
        self.root = root
        self.manifest = manifest
        root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "session.json"
        if self.state_path.exists():
            self.state = _State.model_validate_json(self.state_path.read_bytes())
        else:
            identifier = uuid4().hex
            self.state = _State(
                thread_id=f"guided-{identifier}", campaign_id=f"guided-campaign-{identifier}"
            )
            self._save()
        clean = root / "mock.yaml"
        clean.write_text("{}\n", encoding="utf-8")
        self.config = resolve_runtime_config(
            config_path=clean,
            data_dir=root / "workflow",
            runtime_profile="demo",
            llm_profile="mock",
            embedding_profile="fixture",
            environ={},
        )

    def _save(self) -> None:
        temporary = self.root / "session.next"
        temporary.write_text(self.state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def _view(
        self,
        title: str,
        summary: str,
        *,
        next_step: str = "@OriaTest 帮助",
        sections: tuple[IMSection, ...] = (),
        stage: Literal["confirmation", "result", "waiting_approval", "waiting_event"] = "result",
    ) -> IMViewModel:
        return IMViewModel(
            stage=stage,
            title=f"【演示】{title}",
            summary=f"{BOUNDARY}\n\n{summary}",
            sections=sections,
            detail_ref=self.state.thread_id,
            next_step=next_step,
        )

    async def handle(self, command: DemoInput) -> IMViewModel:
        if command.kind in {"help", "unknown"}:
            return self._view(
                "Oria 核心流程导览",
                "用群内对话运行真实的本地工作流。首版固定华东餐饮、混合报名、10家候选。"
                "审批人与商家/销售/经理均为隔离的合成角色；你控制演示步骤，"
                "不会获得这些角色的生产权限。没有真实发券、投放或商家通知。",
                sections=(
                    IMSection(
                        title="可用说法",
                        text=(
                            "发起招商活动 → 餐饮 → 华东 → 混合报名 → 10家 → 确认发起演示\n"
                            "规则 / 商家 / 状态 / 恢复演示\n"
                            "演示批准 / 模拟商家报名 / 模拟报名结束 / 演示确认 / 模拟选品完成\n"
                            "归因示例 / 证据冲突示例 / 证据不足示例"
                        ),
                    ),
                ),
                next_step="@OriaTest 发起招商活动",
            )
        if command.kind in {"attribution", "conflict", "insufficient"}:
            return await self._attribution(command.kind)
        if command.kind == "slots":
            if self.state.started or self.state.prepared is not None:
                return self._view("已有演示", "请继续当前活动；新演示须使用新的隔离数据目录。")
            router = IntentRouter()
            result: LaunchCampaign | RouterPrompt | object = None
            inputs = (*self.state.inputs, command.slots)
            for item in inputs:
                result = router.route(item, campaign_id=self.state.campaign_id)
            if isinstance(result, LaunchCampaign):
                # Never copy the original input text into Graph state.
                request = result.request.model_copy(
                    update={
                        "user_request": "生成华东餐饮招商活动；混合报名；目标10家；合成演示数据。"
                    }
                )
                self.state = self.state.model_copy(
                    update={
                        "inputs": (),
                        "prepared": result.model_copy(update={"request": request}),
                    }
                )
                self._save()
                return self._view(
                    "请确认活动参数",
                    "华东 · 餐饮 · 混合报名 · 目标10家\n"
                    "确认后在隔离 SQLite 中创建合成活动并执行规则检索、资格过滤和草案。",
                    stage="confirmation",
                    next_step="@OriaTest 确认发起演示",
                )
            if isinstance(result, RouterPrompt):
                self.state = self.state.model_copy(update={"inputs": inputs})
                self._save()
                return self._view(
                    "补充活动需求",
                    result.message,
                    stage="confirmation",
                    next_step="@OriaTest 回复上面的参数；演示依次使用餐饮、华东、混合报名、10家。",
                )
            return self._view("重新输入", "请先发送“发起招商活动”。")
        if command.kind == "start":
            if self.state.started or self.state.prepared is None:
                return self._view("无需重复发起", "请查询当前状态，或先补齐并确认活动参数。")
            request = self.state.prepared.request
            result = await start_local_workflow(
                self.config,
                thread_id=self.state.thread_id,
                campaign_id=self.state.campaign_id,
                user_request=request.user_request,
                workflow_request=request,
            )
            self.state = self.state.model_copy(update={"started": True})
            self._save()
            return self.workflow_view(result)
        if not self.state.started:
            return self._view("尚未发起活动", "先补齐参数并确认发起演示。")
        result = await inspect_local_workflow(self.config, thread_id=self.state.thread_id)
        if command.kind in {"status", "rules", "merchants"}:
            return self.workflow_view(result, focus=command.kind)
        if command.kind == "approve":
            approval = result.view.approval_summary
            if approval is None or approval.status != "pending":
                return self._view("当前无待批演示审批", "请查询状态并按当前等待继续。")
            result = await decide_local_approval(
                self.config,
                thread_id=self.state.thread_id,
                approval_id=approval.approval_id,
                decision="approve",
                reason=None,
            )
        elif command.kind == "confirm":
            pending = next(
                (i for i in result.interrupts if i.get("kind") == "business_confirmation"), None
            )
            if pending is None:
                return self._view("当前无需业务确认", "请查询状态并按当前等待继续。")
            result = await decide_confirmation(
                self.config,
                thread_id=self.state.thread_id,
                confirmation_task_id=str(pending["confirmation_task_id"]),
                decision="confirm",
            )
        elif command.kind in {"enroll", "close"}:
            if not any(i.get("kind") == "enrollment_window" for i in result.interrupts):
                return self._view("当前不在报名窗口", "不会注入与当前阶段不匹配的事件。")
            if command.kind == "enroll":
                result = await inject_merchant_event(
                    self.config,
                    thread_id=self.state.thread_id,
                    source_event_id=f"{self.state.campaign_id}-enrollment",
                    merchant_id="demo-m001",
                    product_ref="synthetic-product-demo-m001",
                )
            else:
                result = await close_enrollment_window(
                    self.config,
                    thread_id=self.state.thread_id,
                    source_event_id=f"{self.state.campaign_id}-close",
                )
        elif command.kind == "selection":
            if not any(i.get("kind") == "selection_event" for i in result.interrupts):
                return self._view("当前不在选品等待", "不会注入与当前阶段不匹配的事件。")
            await inject_selection_decision(
                self.config,
                thread_id=self.state.thread_id,
                source_event_id=f"{self.state.campaign_id}-selection-decision",
                selection_version="selection-v1",
                decision="selected",
                reason_code=None,
            )
            result = await complete_selection(
                self.config,
                thread_id=self.state.thread_id,
                source_event_id=f"{self.state.campaign_id}-selection-complete",
                selection_version="selection-v1",
            )
        return self.workflow_view(result)

    def workflow_view(self, result: LocalWorkflowResult, *, focus: str = "status") -> IMViewModel:
        view = result.view
        next_step = "@OriaTest 状态"
        stage: Literal["result", "waiting_approval", "waiting_event"] = "waiting_event"
        if view.terminal_outcome:
            stage, next_step = "result", "@OriaTest 归因示例"
        elif view.approval_summary and view.approval_summary.status == "pending":
            stage, next_step = (
                "waiting_approval",
                "@OriaTest 演示批准（由合成审批角色执行，保留职责分离）",
            )
        elif view.confirmation_progress:
            next_step = "@OriaTest 演示确认（仅当前一级合成角色；逐级确认）"
        elif any(i.get("kind") == "enrollment_window" for i in result.interrupts):
            next_step = "@OriaTest 模拟商家报名，然后 @OriaTest 模拟报名结束"
        elif any(i.get("kind") == "selection_event" for i in result.interrupts):
            next_step = "@OriaTest 模拟选品完成"
        sections: list[IMSection] = []
        if focus == "rules":
            sections.extend(
                IMSection(
                    title=i.category,
                    text=(f"{i.key_value}\n生效：{i.effective_time}\n来源版本：{i.source_version}"),
                )
                for i in view.rule_summary
            )
            # Five visible sections per existing card contract; users can explicitly
            # request page two through the transport's read-only pagination command.
        elif focus == "merchants":
            sections.extend(
                IMSection(
                    title=i.display_name,
                    text=(
                        f"硬资格：{i.hard_eligibility}；排序：{i.llm_rank}\n{i.recommendation_reason}"
                    ),
                )
                for i in view.merchant_matches.items
            )
            sections.append(
                IMSection(
                    title="硬规则排除",
                    text="\n".join(
                        f"{i.reason}：{i.count}家" for i in view.merchant_exclusion_summary
                    )
                    or "当前无排除记录",
                )
            )
        else:
            sections.append(
                IMSection(title="已完成阶段", text=" → ".join(view.completed_steps) or "开始处理")
            )
            if view.confirmation_progress:
                progress = view.confirmation_progress
                sections.append(
                    IMSection(
                        title="动态确认链",
                        text=(
                            f"第{progress.current_level}/{progress.total_levels}级 · "
                            f"{progress.current_role}"
                        ),
                    )
                )
            if view.selection_summary.submitted_count:
                s = view.selection_summary
                sections.append(
                    IMSection(
                        title="选品与券关联",
                        text=(
                            f"提交{s.submitted_count}件；选中{s.selected_count}件；"
                            f"待定{s.pending_count}件；"
                            f"关联券{s.coupon_linked_count}件"
                        ),
                    )
                )
            if view.placement:
                sections.append(
                    IMSection(
                        title="模拟投放",
                        text=(f"渠道：{view.placement.channel}；状态：{view.placement.status}"),
                    )
                )
            if view.notification_messages:
                sections.append(
                    IMSection(
                        title="模拟商家通知",
                        text="\n".join(
                            f"{i.merchant_id}：{i.status} · {i.message}"
                            for i in view.notification_messages
                        ),
                    )
                )
        return self._view(
            f"{view.stage_index}/{view.stage_total} {view.current_stage}",
            view.pending_action,
            next_step=next_step
            + (
                f"；更多内容：@OriaTest 第2页（共{(len(sections) + 4) // 5}页）"
                if len(sections) > 5
                else ""
            ),
            sections=tuple(sections),
            stage=stage,
        )

    async def _attribution(self, kind: str) -> IMViewModel:
        case = {"attribution": "sb-v1-001", "conflict": "sb-v1-020", "insufficient": "sb-v1-015"}[
            kind
        ]
        result = await run_attribution_ask(
            self.manifest, data_dir=self.root / "attribution", case_id=case
        )
        conclusion = result.conclusion
        if conclusion is None:
            return self._view("归因未形成结论", "受限 Agent 已终止；不会编造解释或执行写工具。")
        sections = [
            IMSection(
                title="有界只读调查",
                text=(
                    f"Mock 回放 · {result.usage.model_turns}轮模型 · "
                    f"{result.usage.tool_calls_total}次工具\n"
                    + " → ".join(i.tool_name for i in result.tool_trace)
                ),
            )
        ]
        sections.extend(
            IMSection(
                title=f"证据{i + 1}",
                text=(f"{e.tool_name} · {e.data_path}\n{json.dumps(e.value, ensure_ascii=False)}")[
                    :2000
                ],
            )
            for i, e in enumerate(conclusion.evidence[:3])
        )
        return self._view(
            "经营异常归因",
            f"{result.question_asked}\n结论类型：{conclusion.outcome}\n"
            f"{conclusion.conclusion or conclusion.confidence_explanation}",
            sections=tuple(sections),
            next_step="@OriaTest 证据冲突示例，或 @OriaTest 证据不足示例",
        )
