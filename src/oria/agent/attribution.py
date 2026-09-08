# ruff: noqa: RUF001
"""Scenario B specialization of the shared bounded research graph."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from oria.agent.graph import build_research_graph
from oria.agent.models import (
    AttributionConclusion,
    attribution_conclusion_schema,
    validate_attribution_conclusion,
    validate_attribution_repair,
)
from oria.agent.spec import ResearchSpec, ResearchStateView
from oria.agent.state import (
    ResearchLimits,
    ResearchRunContext,
    ResearchState,
    initial_research_state,
)
from oria.core.types import JsonValue, Message
from oria.prompts import PromptManager

ATTRIBUTION_TOOL_NAMES = (
    "query_funnel",
    "drill_down",
    "query_activity",
    "query_market_overview",
    "search_history_experience",
)


def _finalize_attribution(
    value: dict[str, JsonValue], state: ResearchStateView
) -> AttributionConclusion:
    tool_results = cast(Mapping[str, Mapping[str, JsonValue]], state.get("tool_results", {}))
    conclusion = validate_attribution_conclusion(
        value, tool_results=tool_results, require_decision=True
    )
    validate_attribution_repair(
        conclusion, state.get("validation_drafts", []), tool_results=tool_results
    )
    return conclusion


def attribution_research_spec() -> ResearchSpec:
    """Return the fixed V0.4 Scenario B specialization."""

    return ResearchSpec(
        prompt_name="attribution_reasoning",
        prompt_version=3,
        tool_names=ATTRIBUTION_TOOL_NAMES,
        response_schema=attribution_conclusion_schema(),
        output_field="conclusion",
        validated_event_type="attribution_validated",
        finalize=_finalize_attribution,
        finalize_on_no_progress=True,
    )


def attribution_research_limits() -> ResearchLimits:
    """Return the Scenario B budget without changing loop termination semantics."""

    return ResearchLimits(max_model_turns=8, max_tool_calls=10, max_validation_repairs=2)


def _tenant_context_message(tenant_id: str) -> Message:
    return Message(
        role="system",
        content=(
            f"当前身份与授权范围：你以 {tenant_id} 租户的身份运行，所有分析工具都只在"
            "当前租户的授权数据范围内查询。你无法通过任何工具参数切换到其他租户，"
            "也不能访问、汇总或比较其他租户的数据。工具返回为空只表示当前租户授权"
            "范围内无匹配记录，不能据此判断其他租户是否存在或不存在该数据。"
        ),
    )


def initial_attribution_state(
    *,
    question: str,
    analysis_period: str,
    conversation_history: Sequence[Message] = (),
    tenant_id: str | None = None,
    prompts: PromptManager | None = None,
) -> ResearchState:
    if not analysis_period.strip():
        raise ValueError("analysis period must be non-empty")
    if any(message.role not in {"user", "assistant"} for message in conversation_history):
        raise ValueError("attribution history only accepts user/assistant messages")
    if len(conversation_history) % 2 != 0 or any(
        message.role != ("user" if index % 2 == 0 else "assistant")
        for index, message in enumerate(conversation_history)
    ):
        raise ValueError("attribution history must contain complete user/assistant pairs")
    state = initial_research_state(
        user_request=question,
        effective_at=analysis_period,
        prompts=prompts,
        spec=attribution_research_spec(),
        prompt_variables={"analysis_period": analysis_period},
    )
    messages: list[dict[str, JsonValue]] = [state["messages"][0]]
    if tenant_id:
        messages.append(_tenant_context_message(tenant_id).model_dump(mode="json"))
    messages.extend(message.model_dump(mode="json") for message in conversation_history)
    messages.append(state["messages"][1])
    state["messages"] = messages
    return state


def build_attribution_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[ResearchState, ResearchRunContext, ResearchState, ResearchState]:
    """Bind Scenario B configuration to the one permanent research loop."""

    return build_research_graph(
        checkpointer=checkpointer,
        spec=attribution_research_spec(),
    )
