"""Deterministic research graph topology and routing tests."""

from __future__ import annotations

import pytest
from langgraph.graph import END

from oria.agent.graph import (
    ResearchNodes,
    build_research_graph,
    route_after_model,
    route_after_tools,
    route_after_validate,
)
from oria.agent.models import campaign_proposal_draft_schema
from oria.agent.state import initial_research_state

pytestmark = pytest.mark.unit


def test_model_draft_schema_excludes_trusted_rule_and_evidence_fields() -> None:
    properties = campaign_proposal_draft_schema().json_schema["properties"]

    assert set(properties) == {
        "schema_version",
        "recommended_merchants",
        "unresolved_items",
        "abstained",
    }


def _state():
    return initial_research_state(
        user_request="生成招商建议",
        effective_at="2026-07-15T00:00:00+08:00",
    )


def test_model_routes_only_from_normalized_structured_and_tool_fields() -> None:
    state = _state()
    state["structured_output"] = {"schema_version": 1}
    assert route_after_model(state) == "validate"

    state = _state()
    state["pending_tool_calls"] = [{"id": "c1", "name": "tool", "args": {}}]
    assert route_after_model(state) == "tools"

    assert route_after_model(_state()) == "validate"
    state = _state()
    state["termination"] = {"status": "failed"}
    assert route_after_model(state) == END


def test_validate_and_tool_routes_stop_only_on_terminal_state() -> None:
    state = _state()
    assert route_after_tools(state) == "model"
    assert route_after_validate(state) == "model"

    state["proposal"] = {"abstained": True}
    assert route_after_validate(state) == END
    state["proposal"] = None
    state["termination"] = {"status": "failed"}
    assert route_after_tools(state) == END
    assert route_after_validate(state) == END


def test_permanent_graph_contains_only_bounded_research_nodes() -> None:
    nodes = build_research_graph().get_graph().nodes
    assert tuple(nodes) == ("__start__", "model", "tools", "validate", "__end__")


@pytest.mark.asyncio
async def test_injected_spy_nodes_visit_only_selected_paths() -> None:
    trace: list[str] = []

    async def model(state, runtime):
        del runtime
        trace.append("model")
        if state["model_turns"] == 0:
            return {
                "model_turns": 1,
                "pending_tool_calls": [{"id": "c1", "name": "tool", "args": {}}],
            }
        return {"model_turns": 2, "structured_output": {"schema_version": 1}}

    async def tools(state, runtime):
        del state, runtime
        trace.append("tools")
        return {"pending_tool_calls": []}

    async def validate(state, runtime):
        del state, runtime
        trace.append("validate")
        return {"proposal": {"abstained": True}}

    graph = build_research_graph(nodes=ResearchNodes(model=model, tools=tools, validate=validate))
    result = await graph.ainvoke(_state())
    assert trace == ["model", "tools", "model", "validate"]
    assert result["proposal"] == {"abstained": True}

    trace.clear()

    async def terminate(state, runtime):
        del state, runtime
        trace.append("model")
        return {"termination": {"status": "failed", "reason": "forced"}}

    async def forbidden(state, runtime):
        del state, runtime
        raise AssertionError("unselected node was visited")

    stopped = build_research_graph(
        nodes=ResearchNodes(model=terminate, tools=forbidden, validate=forbidden)
    )
    result = await stopped.ainvoke(_state())
    assert trace == ["model"]
    assert result["termination"]["reason"] == "forced"
