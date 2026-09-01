from __future__ import annotations

from typing import Any

import pytest

from oria.core.types import NodeResult
from oria.orchestrator.patterns import (
    evaluator_optimizer,
    orchestrator_workers,
    parallelization,
    prompt_chaining,
    routing,
)

pytestmark = pytest.mark.unit


class FakeNode:
    def __init__(self, name: str, updates: dict[str, Any] | None = None) -> None:
        self.name = name
        self.updates = updates or {"node": name}
        self.calls = 0

    async def execute(self, state: dict[str, Any], ctx: Any) -> NodeResult:
        del state, ctx
        self.calls += 1
        return NodeResult(status="completed", updates=self.updates)


def _state() -> dict[str, Any]:
    return {
        "messages": [],
        "plan": {"goal": "test", "steps": []},
        "results": {},
        "approvals": {},
        "external_waits": {},
        "meta": {
            "tenant_id": "tenant-a",
            "session_id": "session-a",
            "thread_id": "thread-a",
            "run_id": "run-a",
            "job_id": None,
            "requester_subject_id": "operator-a",
        },
    }


@pytest.mark.asyncio
async def test_routing_executes_only_classified_branch() -> None:
    left = FakeNode("left")
    right = FakeNode("right")
    graph = routing({"left": left, "right": right}, lambda state: "right")

    result = await graph.ainvoke(_state(), context=None)

    assert left.calls == 0
    assert right.calls == 1
    assert result["results"]["right"].updates["node"] == "right"


@pytest.mark.asyncio
async def test_parallelization_fans_out_and_concatenates_in_declaration_order() -> None:
    first = FakeNode("first")
    second = FakeNode("second")
    graph = parallelization([first, second])

    result = await graph.ainvoke(_state(), context=None)

    assert first.calls == second.calls == 1
    joined = result["results"]["parallel_join"].updates["items"]
    assert [item["updates"]["node"] for item in joined] == ["first", "second"]


@pytest.mark.asyncio
async def test_orchestrator_workers_dispatches_dynamic_plan() -> None:
    planner = FakeNode(
        "planner",
        {
            "plan": {
                "goal": "fan out",
                "steps": [
                    {"node_id": "worker_a", "params": {"value": 1}},
                    {"node_id": "worker_b", "params": {"value": 2}},
                ],
            }
        },
    )
    workers: list[FakeNode] = []

    def factory(task: dict[str, Any]) -> FakeNode:
        node = FakeNode(task["node_id"], {"params": task["params"]})
        workers.append(node)
        return node

    graph = orchestrator_workers(planner, factory)

    result = await graph.ainvoke(_state(), context=None)

    assert {key for key in result["results"]} == {"planner", "worker_a", "worker_b"}
    assert len(workers) == 2


@pytest.mark.asyncio
async def test_evaluator_optimizer_repeats_until_threshold() -> None:
    generator = FakeNode("generator")
    graph = evaluator_optimizer(generator, lambda state: generator.calls / 2, threshold=1.0)

    result = await graph.ainvoke(_state(), context=None, config={"recursion_limit": 5})

    assert generator.calls == 2
    assert result["results"]["generator"].status == "completed"


@pytest.mark.asyncio
async def test_prompt_chaining_stops_when_gate_rejects_transition() -> None:
    first = FakeNode("first")
    second = FakeNode("second")
    graph = prompt_chaining([first, second], gates=[lambda state: False])

    result = await graph.ainvoke(_state(), context=None)

    assert first.calls == 1
    assert second.calls == 0
    assert set(result["results"]) == {"first"}
