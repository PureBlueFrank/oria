"""Scenario B specialization of the shared bounded research graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from oria.agent.graph import build_research_graph
from oria.agent.models import (
    AttributionConclusion,
    attribution_conclusion_schema,
    validate_attribution_conclusion,
)
from oria.agent.spec import ResearchSpec, ResearchStateView
from oria.agent.state import (
    ResearchLimits,
    ResearchRunContext,
    ResearchState,
    initial_research_state,
)
from oria.core.types import JsonValue
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
    return validate_attribution_conclusion(value, tool_results=tool_results)


def attribution_research_spec() -> ResearchSpec:
    """Return the fixed V0.4 Scenario B specialization."""

    return ResearchSpec(
        prompt_name="attribution_reasoning",
        prompt_version=1,
        tool_names=ATTRIBUTION_TOOL_NAMES,
        response_schema=attribution_conclusion_schema(),
        output_field="conclusion",
        validated_event_type="attribution_validated",
        finalize=_finalize_attribution,
    )


def attribution_research_limits() -> ResearchLimits:
    """Return the Scenario B budget without changing loop termination semantics."""

    return ResearchLimits(max_model_turns=8, max_tool_calls=6)


def initial_attribution_state(
    *,
    question: str,
    analysis_period: str,
    prompts: PromptManager | None = None,
) -> ResearchState:
    if not analysis_period.strip():
        raise ValueError("analysis period must be non-empty")
    return initial_research_state(
        user_request=question,
        effective_at=analysis_period,
        prompts=prompts,
        spec=attribution_research_spec(),
        prompt_variables={"analysis_period": analysis_period},
    )


def build_attribution_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[ResearchState, ResearchRunContext, ResearchState, ResearchState]:
    """Bind Scenario B configuration to the one permanent research loop."""

    return build_research_graph(
        checkpointer=checkpointer,
        spec=attribution_research_spec(),
    )
