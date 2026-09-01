"""Convenience builders for the five reviewed LangGraph workflow patterns."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeAlias, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from oria.core.context import Context
from oria.core.protocols import Node
from oria.core.types import JsonValue, NodeResult
from oria.orchestrator.state import Plan, Step, WorkflowState

Graph: TypeAlias = CompiledStateGraph[WorkflowState, Context, WorkflowState, WorkflowState]
Task: TypeAlias = Step
Score: TypeAlias = float


def _name(node: Node, index: int, prefix: str) -> str:
    configured = getattr(node, "name", None)
    if isinstance(configured, str) and configured:
        return configured
    return f"{prefix}_{index}"


def _node_action(node_id: str, node: Node) -> Callable[..., Any]:
    async def action(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
        result = await node.execute(cast(dict[str, Any], dict(state)), runtime.context)
        return {"results": {node_id: result}}

    return action


def _compile(builder: StateGraph[WorkflowState, Context, Any, Any], name: str) -> Graph:
    return cast(Graph, builder.compile(name=name))


def routing(
    branches: dict[str, Node],
    classifier: Callable[[WorkflowState], str],
) -> Graph:
    """Compile a graph that classifies once and executes exactly one branch."""

    if not branches:
        raise ValueError("routing requires at least one branch")
    builder = StateGraph(WorkflowState, context_schema=Context)
    for branch, node in branches.items():
        if not branch:
            raise ValueError("routing branch names must be non-empty")
        builder.add_node(branch, _node_action(branch, node))

    def select(state: WorkflowState) -> str:
        selected = classifier(state)
        if selected not in branches:
            raise ValueError("routing classifier selected an unknown branch")
        return selected

    builder.add_conditional_edges(START, select, {name: name for name in branches})
    for branch in branches:
        builder.add_edge(branch, END)
    return _compile(builder, "routing")


def parallelization(
    nodes: list[Node],
    join: Literal["concat", "merge"] = "concat",
) -> Graph:
    """Compile a deterministic fan-out/barrier graph over independent nodes."""

    if not nodes:
        raise ValueError("parallelization requires at least one node")
    if join not in {"concat", "merge"}:
        raise ValueError("parallelization join must be concat or merge")
    names = [_name(node, index, "parallel") for index, node in enumerate(nodes)]
    if len(set(names)) != len(names):
        raise ValueError("parallelization node names must be unique")
    builder = StateGraph(WorkflowState, context_schema=Context)
    for node_id, node in zip(names, nodes, strict=True):
        builder.add_node(node_id, _node_action(node_id, node))
        builder.add_edge(START, node_id)
    if join == "merge":
        builder.add_edge(names, END)
    else:

        async def concat_results(state: WorkflowState) -> dict[str, object]:
            items = [state["results"][node_id].model_dump(mode="json") for node_id in names]
            result = NodeResult(status="completed", updates={"items": cast(JsonValue, items)})
            return {"results": {"parallel_join": result}}

        builder.add_node("parallel_join", concat_results)
        builder.add_edge(names, "parallel_join")
        builder.add_edge("parallel_join", END)
    return _compile(builder, "parallelization")


def orchestrator_workers(
    planner: Node,
    worker_factory: Callable[[Task], Node],
) -> Graph:
    """Compile planner-driven dynamic fan-out using official ``Send`` packets."""

    builder = StateGraph(WorkflowState, context_schema=Context)

    async def plan_action(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
        result = await planner.execute(cast(dict[str, Any], dict(state)), runtime.context)
        raw_plan = result.updates.get("plan")
        if not isinstance(raw_plan, Mapping):
            raise ValueError("planner result must contain a plan update")
        plan = cast(Plan, dict(raw_plan))
        if not isinstance(plan.get("goal"), str) or not isinstance(plan.get("steps"), list):
            raise ValueError("planner produced an invalid plan")
        return {"plan": plan, "results": {"planner": result}}

    async def worker_action(state: dict[str, Any], runtime: Runtime[Context]) -> dict[str, object]:
        raw_task = state.get("_oria_task")
        if not isinstance(raw_task, Mapping):
            raise ValueError("worker dispatch is missing its task")
        task = cast(Task, dict(raw_task))
        node_id = task.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("worker task requires a node_id")
        result = await worker_factory(task).execute(dict(state), runtime.context)
        return {"results": {node_id: result}}

    def dispatch(state: WorkflowState) -> list[Send] | str:
        steps = state["plan"]["steps"]
        if not steps:
            return END
        return [Send("worker", {**state, "_oria_task": dict(task)}) for task in steps]

    builder.add_node("planner", plan_action)
    builder.add_node("worker", worker_action)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", dispatch)
    builder.add_edge("worker", END)
    return _compile(builder, "orchestrator_workers")


def evaluator_optimizer(
    generator: Node,
    evaluator: Callable[[WorkflowState], Score],
    threshold: float,
) -> Graph:
    """Compile a generator/evaluator loop that exits once the score reaches threshold."""

    if not math_is_finite(threshold):
        raise ValueError("evaluator threshold must be finite")
    builder = StateGraph(WorkflowState, context_schema=Context)
    builder.add_node("generator", _node_action("generator", generator))

    async def evaluate(state: WorkflowState) -> str:
        score = evaluator(state)
        if inspect.isawaitable(score):
            score = await score
        numeric = float(score)
        if not math_is_finite(numeric):
            raise ValueError("evaluator score must be finite")
        return END if numeric >= threshold else "generator"

    builder.add_edge(START, "generator")
    builder.add_conditional_edges("generator", evaluate)
    return _compile(builder, "evaluator_optimizer")


def prompt_chaining(
    steps: list[Node],
    gates: list[Callable[[WorkflowState], bool]] | None = None,
) -> Graph:
    """Compile a linear node chain with an optional gate on each transition."""

    if not steps:
        raise ValueError("prompt_chaining requires at least one step")
    selected_gates = gates or []
    if selected_gates and len(selected_gates) != len(steps) - 1:
        raise ValueError("prompt_chaining gates must match the number of transitions")
    names = [_name(node, index, "step") for index, node in enumerate(steps)]
    if len(set(names)) != len(names):
        raise ValueError("prompt_chaining step names must be unique")
    builder = StateGraph(WorkflowState, context_schema=Context)
    for node_id, node in zip(names, steps, strict=True):
        builder.add_node(node_id, _node_action(node_id, node))
    builder.add_edge(START, names[0])
    for index, source in enumerate(names[:-1]):
        target = names[index + 1]
        if not selected_gates:
            builder.add_edge(source, target)
            continue
        gate = selected_gates[index]

        def route(
            state: WorkflowState,
            *,
            selected_gate: Callable[[WorkflowState], bool] = gate,
            selected_target: str = target,
        ) -> str:
            return selected_target if selected_gate(state) else END

        builder.add_conditional_edges(source, route, {target: target, END: END})
    builder.add_edge(names[-1], END)
    return _compile(builder, "prompt_chaining")


def math_is_finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}
