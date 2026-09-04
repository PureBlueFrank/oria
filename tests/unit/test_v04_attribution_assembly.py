"""V0.4-T03 shared-loop assembly contracts."""

from __future__ import annotations

import inspect

import pytest

import oria.agent.attribution as attribution_module
from oria.agent import (
    ATTRIBUTION_TOOL_NAMES,
    attribution_research_limits,
    attribution_research_spec,
    build_attribution_graph,
    initial_attribution_state,
)

pytestmark = pytest.mark.unit


def test_attribution_specialization_reuses_the_only_research_graph() -> None:
    graph = build_attribution_graph()
    spec = attribution_research_spec()

    assert tuple(graph.get_graph().nodes) == (
        "__start__",
        "model",
        "tools",
        "validate",
        "__end__",
    )
    assert spec.tool_names == ATTRIBUTION_TOOL_NAMES
    assert spec.prompt_name == "attribution_reasoning"
    assert spec.prompt_version == 1
    assert "research_model_node" not in inspect.getsource(attribution_module)
    assert "research_tools_node" not in inspect.getsource(attribution_module)
    assert "research_validate_node" not in inspect.getsource(attribution_module)


def test_attribution_initial_state_and_budget_are_scenario_specific() -> None:
    state = initial_attribution_state(
        question="Why did conversion change?",
        analysis_period="2026-08-30/2026-08-31",
    )
    limits = attribution_research_limits()

    assert state["messages"][0]["role"] == "system"
    assert "2026-08-30/2026-08-31" in state["messages"][0]["content"]
    assert state["messages"][1]["content"] == "Why did conversion change?"
    assert state["conclusion"] is None
    assert limits.max_model_turns == 8
    assert limits.max_tool_calls == 6
