"""Deterministic phrase routing for the first local chat interface."""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypeAlias

from pydantic import Field

from oria.core.types import ValueModel
from oria.orchestrator.local_executor import default_request
from oria.orchestrator.scenario_a import ScenarioAWorkflowRequest

EnrollmentMode = Literal["merchant", "auto", "hybrid"]
InterruptKind = Literal[
    "launch_approval",
    "consumer_publish_approval",
    "business_confirmation",
    "enrollment_window",
    "selection_event",
    "workflow_handoff",
]


class ActiveInterrupt(ValueModel):
    """Minimum public interrupt context used to disambiguate decisions."""

    kind: InterruptKind
    identifier: str = Field(min_length=1)


class LaunchCampaign(ValueModel):
    action: Literal["launch_campaign"] = "launch_campaign"
    categories: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = Field(min_length=1)
    enrollment_mode: EnrollmentMode
    target_count: int = Field(ge=1, le=100)
    request: ScenarioAWorkflowRequest


class QueryStatus(ValueModel):
    action: Literal["query_status"] = "query_status"


class DecideApproval(ValueModel):
    action: Literal["decide_approval"] = "decide_approval"
    approval_id: str = Field(min_length=1)
    approval_kind: Literal["launch_approval", "consumer_publish_approval"]
    decision: Literal["approve", "reject"]


class DecideConfirmation(ValueModel):
    action: Literal["decide_confirmation"] = "decide_confirmation"
    confirmation_task_id: str = Field(min_length=1)
    decision: Literal["confirm", "reject"]


class HelpAction(ValueModel):
    action: Literal["help"] = "help"


class QuitAction(ValueModel):
    action: Literal["quit"] = "quit"


class NewSessionAction(ValueModel):
    action: Literal["new_session"] = "new_session"


class RouterPrompt(ValueModel):
    action: Literal["prompt"] = "prompt"
    missing_slot: Literal["category", "city", "enrollment_mode", "target_count", "context"]
    message: str = Field(min_length=1)


class UnsupportedInput(ValueModel):
    action: Literal["unsupported"] = "unsupported"
    message: str = Field(min_length=1)


RoutedAction: TypeAlias = Annotated[
    LaunchCampaign
    | QueryStatus
    | DecideApproval
    | DecideConfirmation
    | HelpAction
    | QuitAction
    | NewSessionAction
    | RouterPrompt
    | UnsupportedInput,
    Field(discriminator="action"),
]


class _LaunchSlots(ValueModel):
    original_text: str = Field(min_length=1)
    categories: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    enrollment_mode: EnrollmentMode | None = None
    target_count: int | None = Field(default=None, ge=1, le=100)


_KNOWN_CATEGORIES = (
    "餐饮",
    "正餐",
    "美食",
    "零售",
    "酒店",
    "医美",
    "休闲娱乐",
)
_KNOWN_LOCATIONS = (
    "华东",
    "华北",
    "华南",
    "华中",
    "西南",
    "西北",
    "东北",
    "上海",
    "杭州",
    "南京",
    "苏州",
    "北京",
    "广州",
    "深圳",
    "成都",
    "武汉",
)
_MODE_TERMS: tuple[tuple[EnrollmentMode, tuple[str, ...]], ...] = (
    ("hybrid", ("hybrid", "混合报名", "混合模式")),
    ("merchant", ("merchant", "商家自主", "自主报名")),
    ("auto", ("auto", "自动圈品", "系统自动")),
)


class IntentRouter:
    """Stateful, deterministic router with one-at-a-time launch slot prompts."""

    def __init__(self) -> None:
        self._pending_launch: _LaunchSlots | None = None

    def reset(self) -> None:
        self._pending_launch = None

    def route(
        self,
        text: str,
        *,
        campaign_id: str,
        active_interrupt: ActiveInterrupt | None = None,
    ) -> RoutedAction:
        normalized = text.strip()
        if not normalized:
            return UnsupportedInput(message="请输入需求, 或输入 /help 查看可用说法。")

        command = normalized.lower()
        if command == "/help":
            return HelpAction()
        if command == "/quit":
            return QuitAction()
        if command == "/new":
            self.reset()
            return NewSessionAction()
        if command == "/status" or self._is_status(normalized):
            return QueryStatus()

        if self._pending_launch is not None:
            return self._continue_launch(normalized, campaign_id=campaign_id)
        if self._is_launch(normalized):
            self._pending_launch = self._extract_launch_slots(normalized)
            return self._finish_or_prompt(campaign_id=campaign_id)

        if normalized in {"批准", "通过", "拒绝"}:
            if active_interrupt is None or active_interrupt.kind not in {
                "launch_approval",
                "consumer_publish_approval",
            }:
                return RouterPrompt(
                    missing_slot="context",
                    message=(
                        "当前没有待处理的平台审批; 请先查看状态, 不能脱离当前中断猜测审批对象。"
                    ),
                )
            approval_kind: Literal["launch_approval", "consumer_publish_approval"] = (
                "launch_approval"
                if active_interrupt.kind == "launch_approval"
                else "consumer_publish_approval"
            )
            return DecideApproval(
                approval_id=active_interrupt.identifier,
                approval_kind=approval_kind,
                decision="reject" if normalized == "拒绝" else "approve",
            )

        if normalized in {"确认", "驳回"}:
            if active_interrupt is None or active_interrupt.kind != "business_confirmation":
                return RouterPrompt(
                    missing_slot="context",
                    message="当前没有待处理的业务确认; 请先查看状态, 不能猜测确认任务。",
                )
            return DecideConfirmation(
                confirmation_task_id=active_interrupt.identifier,
                decision="reject" if normalized == "驳回" else "confirm",
            )

        return UnsupportedInput(
            message=(
                "我没有识别这条指令。可说“发起…招商活动”“状态”“批准/拒绝”"
                "或“确认/驳回”; 输入 /help 查看完整帮助。"
            )
        )

    @staticmethod
    def _is_launch(text: str) -> bool:
        return bool(re.search(r"(?:发起.*招商活动|创建.*(?:招商)?活动)", text))

    @staticmethod
    def _is_status(text: str) -> bool:
        return text == "状态" or "现在到哪了" in text or "进行到" in text

    def _continue_launch(self, text: str, *, campaign_id: str) -> RoutedAction:
        pending = self._pending_launch
        if pending is None:
            raise RuntimeError("launch slot state is unavailable")
        updates: dict[str, object] = {}
        if not pending.categories:
            updates["categories"] = self._extract_categories(text)
        elif not pending.cities:
            updates["cities"] = self._extract_cities(text)
        elif pending.enrollment_mode is None:
            updates["enrollment_mode"] = self._extract_mode(text)
        elif pending.target_count is None:
            updates["target_count"] = self._extract_target_count(text)
        self._pending_launch = pending.model_copy(update=updates)
        return self._finish_or_prompt(campaign_id=campaign_id)

    def _finish_or_prompt(self, *, campaign_id: str) -> RoutedAction:
        pending = self._pending_launch
        if pending is None:
            raise RuntimeError("launch slot state is unavailable")
        if not pending.categories:
            return RouterPrompt(missing_slot="category", message="招商类目是什么? 例如: 餐饮。")
        if not pending.cities:
            return RouterPrompt(
                missing_slot="city", message="招商城市或区域是什么? 例如: 上海、杭州或华东。"
            )
        if pending.enrollment_mode is None:
            return RouterPrompt(
                missing_slot="enrollment_mode",
                message="报名模式要 merchant、auto、hybrid 哪种?",
            )
        if pending.target_count is None:
            return RouterPrompt(
                missing_slot="target_count", message="目标招商数量是多少家? 请输入 1-100。"
            )

        request_text = (
            f"{pending.original_text}; 招商类目: {'、'.join(pending.categories)}; "
            f"招商城市/区域: {'、'.join(pending.cities)}; 报名模式: {pending.enrollment_mode}; "
            f"目标数量: {pending.target_count} 家"
        )
        base = default_request(campaign_id=campaign_id, user_request=request_text)
        request = ScenarioAWorkflowRequest.model_validate(
            {
                **base.model_dump(mode="python"),
                "max_candidates": pending.target_count,
                "enrollment_mode": pending.enrollment_mode,
                "placement_spec": {
                    **base.placement_spec,
                    "region": ",".join(pending.cities),
                },
            }
        )
        action = LaunchCampaign(
            categories=pending.categories,
            cities=pending.cities,
            enrollment_mode=pending.enrollment_mode,
            target_count=pending.target_count,
            request=request,
        )
        self.reset()
        return action

    def _extract_launch_slots(self, text: str) -> _LaunchSlots:
        return _LaunchSlots(
            original_text=text,
            categories=self._extract_categories(text),
            cities=self._extract_cities(text),
            enrollment_mode=self._extract_mode(text),
            target_count=self._extract_target_count(text),
        )

    @staticmethod
    def _extract_categories(text: str) -> tuple[str, ...]:
        explicit = re.search(
            r"(?:类目|品类)(?:是|为|[:\uff1a])?\s*([^\s\uff0c,\u3002\uff1b;]+)", text
        )
        if explicit:
            return (explicit.group(1),)
        return tuple(category for category in _KNOWN_CATEGORIES if category in text)

    @staticmethod
    def _extract_cities(text: str) -> tuple[str, ...]:
        found = tuple(location for location in _KNOWN_LOCATIONS if location in text)
        if found:
            return found
        explicit = re.search(r"(?:城市|地区|区域)(?:是|为|[:\uff1a])?\s*([^\u3002\uff1b;]+)", text)
        if not explicit:
            return ()
        values = tuple(
            value.strip()
            for value in re.split(r"[\u3001\uff0c,]", explicit.group(1))
            if value.strip()
        )
        return values

    @staticmethod
    def _extract_mode(text: str) -> EnrollmentMode | None:
        lowered = text.lower()
        for mode, terms in _MODE_TERMS:
            if any(term in lowered for term in terms):
                return mode
        return None

    @staticmethod
    def _extract_target_count(text: str) -> int | None:
        matched = re.search(r"(?<!\d)(\d{1,3})\s*家", text)
        if not matched:
            stripped = text.strip()
            if not re.fullmatch(r"\d{1,3}", stripped):
                return None
            raw = int(stripped)
        else:
            raw = int(matched.group(1))
        return raw if 1 <= raw <= 100 else None
