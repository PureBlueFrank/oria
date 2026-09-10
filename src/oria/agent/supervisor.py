"""Checkpoint-safe values and state for deterministic subagent supervision."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import Field, model_validator

from oria.agent.attribution import (
    attribution_research_limits,
    attribution_research_spec,
    initial_attribution_state,
)
from oria.agent.graph import build_research_graph, campaign_research_spec
from oria.agent.models import AgentTermination
from oria.agent.spec import ResearchSpec
from oria.agent.state import ResearchLimits, ResearchRunContext, initial_research_state
from oria.core.context import Context
from oria.core.types import JsonValue, Message, ValueModel
from oria.orchestrator.checkpoint import checkpoint_config
from oria.permission.tools import authorized_tool_names

DEFAULT_MAX_HANDOFFS = 2
SubagentName = Literal["campaign_research", "attribution_research"]

_ATTRIBUTION_TERMS = (
    "attribution",
    "root cause",
    "why",
    "归因",
    "原因",
    "为什么",
    "下跌",
    "下降",
    "异常",
    "漏斗",
    "转化率",
)
_CAMPAIGN_TERMS = (
    "campaign",
    "merchant",
    "recruitment",
    "招商",
    "商家",
    "活动",
    "选品",
    "优惠",
    "券",
)


class SupervisorHandoff(ValueModel):
    """Fixed delegation envelope persisted before a subagent is invoked."""

    schema_version: Literal[1] = 1
    subagent_name: SubagentName
    task: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)


class SubagentUsage(ValueModel):
    """Stable usage projection returned across the supervisor seam."""

    model_turns: int = Field(default=0, ge=0)
    tool_calls_total: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_cost: float = Field(default=0.0, ge=0)


class SubagentResult(ValueModel):
    """Fixed result envelope for successful and terminated subagent runs."""

    schema_version: Literal[1] = 1
    subagent_name: SubagentName
    status: Literal["completed", "failed", "waiting"]
    result: dict[str, JsonValue] | None = None
    termination: AgentTermination | None = None
    usage: SubagentUsage = SubagentUsage()
    allowlisted_tools: tuple[str, ...]
    visible_tools: tuple[str, ...]

    @model_validator(mode="after")
    def validate_outcome(self) -> SubagentResult:
        if not set(self.visible_tools).issubset(self.allowlisted_tools):
            raise ValueError("visible tools must be a subset of the subagent allowlist")
        if self.status == "completed":
            if self.result is None or self.termination is not None:
                raise ValueError("completed subagent results require a result only")
        elif self.result is not None or self.termination is None:
            raise ValueError("terminated subagent results require termination only")
        elif self.status != self.termination.status:
            raise ValueError("subagent status must match termination status")
        return self


class SupervisorTermination(ValueModel):
    """Explicit supervisor stop reason, including recovered subagent failures."""

    status: Literal["failed", "waiting"]
    reason: str = Field(min_length=1)
    handoffs: int = Field(ge=0)
    last_subagent: SubagentName | None = None


class SupervisorRouteDecision(ValueModel):
    """Replayable result of the fixed tool-style request classifier."""

    subagent_name: SubagentName
    reason: Literal["attribution_term", "campaign_term"]


class SupervisorState(TypedDict):
    user_request: str
    effective_at: str
    max_candidates: int
    model_turns: int
    handoffs: list[dict[str, JsonValue]]
    max_handoffs: int
    active_handoff: dict[str, JsonValue] | None
    subagent_results: list[dict[str, JsonValue]]
    route_reason: str | None
    final_result: dict[str, JsonValue] | None
    termination: dict[str, JsonValue] | None
    conversation_history: NotRequired[list[dict[str, JsonValue]]]
    events: NotRequired[list[dict[str, JsonValue]]]


class SupervisorRouter:
    """Deterministic classifier that never delegates routing to an LLM."""

    def route(self, request: str) -> SupervisorRouteDecision | None:
        normalized = request.strip().casefold()
        if any(term in normalized for term in _ATTRIBUTION_TERMS):
            return SupervisorRouteDecision(
                subagent_name="attribution_research",
                reason="attribution_term",
            )
        if any(term in normalized for term in _CAMPAIGN_TERMS):
            return SupervisorRouteDecision(
                subagent_name="campaign_research",
                reason="campaign_term",
            )
        return None


@dataclass(frozen=True, slots=True)
class SupervisorRunContext:
    """Trusted context and bounded budgets passed to supervisor nodes."""

    ctx: Context
    campaign_limits: ResearchLimits = field(default_factory=ResearchLimits)
    attribution_limits: ResearchLimits = field(default_factory=attribution_research_limits)


SubagentInvoker: TypeAlias = Callable[
    [SupervisorHandoff, SupervisorState, SupervisorRunContext], Awaitable[SubagentResult]
]


def initial_supervisor_state(
    *,
    user_request: str,
    effective_at: str,
    max_candidates: int = 10,
    max_handoffs: int = DEFAULT_MAX_HANDOFFS,
    conversation_history: tuple[Message, ...] = (),
) -> SupervisorState:
    """Build a fully defaulted JSON-serializable supervisor state."""

    if not user_request.strip():
        raise ValueError("user request must be non-empty")
    if not effective_at.strip():
        raise ValueError("effective at must be non-empty")
    if max_candidates < 1 or max_candidates > 100:
        raise ValueError("max candidates must be between 1 and 100")
    if max_handoffs < 1 or max_handoffs > DEFAULT_MAX_HANDOFFS:
        raise ValueError("max handoffs must be between one and the fixed upper bound")
    return cast(
        SupervisorState,
        {
            "user_request": user_request,
            "effective_at": effective_at,
            "max_candidates": max_candidates,
            "model_turns": 0,
            "handoffs": [],
            "max_handoffs": max_handoffs,
            "active_handoff": None,
            "subagent_results": [],
            "route_reason": None,
            "final_result": None,
            "termination": None,
            "conversation_history": [
                cast(dict[str, JsonValue], message.model_dump(mode="json"))
                for message in conversation_history
            ],
            "events": [],
        },
    )


def _event(
    state: SupervisorState, event_type: str, **values: JsonValue
) -> list[dict[str, JsonValue]]:
    return [*state.get("events", []), {"type": event_type, **values}]


def _dump_value(value: ValueModel) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], value.model_dump(mode="json"))


def _supervisor_termination(
    state: SupervisorState,
    *,
    status: Literal["failed", "waiting"],
    reason: str,
    last_subagent: SubagentName | None = None,
) -> dict[str, JsonValue]:
    return _dump_value(
        SupervisorTermination(
            status=status,
            reason=reason,
            handoffs=len(state["handoffs"]),
            last_subagent=last_subagent,
        )
    )


def _subagent_specs() -> Mapping[SubagentName, ResearchSpec]:
    return {
        "campaign_research": campaign_research_spec(),
        "attribution_research": attribution_research_spec(),
    }


def _child_config(ctx: Context, handoff: SupervisorHandoff, handoff_index: int) -> RunnableConfig:
    config = checkpoint_config(ctx)
    configurable = config["configurable"]
    configurable["checkpoint_ns"] = (
        f"supervisor/{ctx.run_id}/{handoff_index}/{handoff.subagent_name}"
    )
    return config


def _research_termination(reason: str, limits: ResearchLimits) -> AgentTermination:
    return AgentTermination(
        status="failed",
        reason=reason,
        limits=cast(dict[str, JsonValue], limits.model_dump(mode="json")),
        observed_usage={
            "model_turns": 0,
            "tool_calls_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        },
    )


async def authorized_subagent_tools(spec: ResearchSpec, ctx: Context) -> tuple[str, ...]:
    """Intersect a spec allowlist with the caller's policy-authorized tools."""

    if not set(spec.tool_names).issubset(set(ctx.tools)):
        raise LookupError("subagent allowlist contains unavailable tools")
    caller_visible = frozenset(await authorized_tool_names(ctx.tools, ctx))
    visible = tuple(name for name in spec.tool_names if name in caller_visible)
    if not set(visible).issubset(spec.tool_names) or not set(visible).issubset(caller_visible):
        raise AssertionError("subagent tool visibility expanded beyond its security intersection")
    return visible


def _usage_from_state(state: Mapping[str, object]) -> SubagentUsage:
    return SubagentUsage(
        model_turns=cast(int, state.get("model_turns", 0)),
        tool_calls_total=cast(int, state.get("tool_calls_total", 0)),
        input_tokens=cast(int, state.get("input_tokens", 0)),
        output_tokens=cast(int, state.get("output_tokens", 0)),
        total_cost=cast(float, state.get("total_cost", 0.0)),
    )


async def _invoke_research_subagent(
    handoff: SupervisorHandoff,
    state: SupervisorState,
    context: SupervisorRunContext,
    *,
    graphs: Mapping[SubagentName, CompiledStateGraph[Any, Any, Any, Any]],
    specs: Mapping[SubagentName, ResearchSpec],
) -> SubagentResult:
    spec = specs[handoff.subagent_name]
    limits = (
        context.campaign_limits
        if handoff.subagent_name == "campaign_research"
        else context.attribution_limits
    )
    try:
        visible_tools = await authorized_subagent_tools(spec, context.ctx)
    except LookupError:
        termination = _research_termination("subagent_tools_unavailable", limits)
        return SubagentResult(
            subagent_name=handoff.subagent_name,
            status="failed",
            termination=termination,
            allowlisted_tools=spec.tool_names,
            visible_tools=(),
        )

    if handoff.subagent_name == "campaign_research":
        child_state = initial_research_state(
            user_request=handoff.task,
            effective_at=state["effective_at"],
            max_candidates=state["max_candidates"],
            spec=spec,
        )
    else:
        child_state = initial_attribution_state(
            question=handoff.task,
            analysis_period=state["effective_at"],
            conversation_history=tuple(
                Message.model_validate(message) for message in state.get("conversation_history", [])
            ),
            tenant_id=context.ctx.tenant_id,
        )
    try:
        output = await graphs[handoff.subagent_name].ainvoke(
            child_state,
            config=_child_config(context.ctx, handoff, len(state["handoffs"])),
            context=ResearchRunContext(ctx=context.ctx, limits=limits),
        )
    except Exception:
        termination = _research_termination("subagent_invocation_failed", limits)
        return SubagentResult(
            subagent_name=handoff.subagent_name,
            status="failed",
            termination=termination,
            allowlisted_tools=spec.tool_names,
            visible_tools=visible_tools,
        )

    usage = _usage_from_state(output)
    raw_termination = output.get("termination")
    if raw_termination is not None:
        termination = AgentTermination.model_validate(raw_termination)
        return SubagentResult(
            subagent_name=handoff.subagent_name,
            status=termination.status,
            termination=termination,
            usage=usage,
            allowlisted_tools=spec.tool_names,
            visible_tools=visible_tools,
        )
    raw_result = output.get(spec.output_field) or output.get("final_result")
    if not isinstance(raw_result, dict):
        termination = _research_termination("subagent_result_missing", limits)
        return SubagentResult(
            subagent_name=handoff.subagent_name,
            status="failed",
            termination=termination,
            usage=usage,
            allowlisted_tools=spec.tool_names,
            visible_tools=visible_tools,
        )
    return SubagentResult(
        subagent_name=handoff.subagent_name,
        status="completed",
        result=cast(dict[str, JsonValue], raw_result),
        usage=usage,
        allowlisted_tools=spec.tool_names,
        visible_tools=visible_tools,
    )


async def supervisor_route_node(
    state: SupervisorState,
    runtime: Runtime[SupervisorRunContext],
    *,
    router: SupervisorRouter,
) -> dict[str, object]:
    """Create exactly one deterministic handoff, subject to the hard limit."""

    if runtime.context is None:
        raise RuntimeError("supervisor run context is required")
    max_handoffs = min(state["max_handoffs"], DEFAULT_MAX_HANDOFFS)
    if len(state["handoffs"]) >= max_handoffs:
        return {
            "termination": _supervisor_termination(
                state,
                status="failed",
                reason="max_handoffs_exceeded",
            ),
            "events": _event(state, "supervisor_stopped", reason="max_handoffs_exceeded"),
        }
    decision = router.route(state["user_request"])
    if decision is None:
        return {
            "termination": _supervisor_termination(
                state,
                status="failed",
                reason="unsupported_request",
            ),
            "events": _event(state, "supervisor_stopped", reason="unsupported_request"),
        }
    handoff = SupervisorHandoff(
        subagent_name=decision.subagent_name,
        task=state["user_request"],
        source_run_id=runtime.context.ctx.run_id,
    )
    dumped_handoff = _dump_value(handoff)
    return {
        "active_handoff": dumped_handoff,
        "handoffs": [*state["handoffs"], dumped_handoff],
        "route_reason": decision.reason,
        "events": _event(
            state,
            "supervisor_handoff_created",
            subagent_name=decision.subagent_name,
            route_reason=decision.reason,
        ),
    }


async def supervisor_subagent_node(
    state: SupervisorState,
    runtime: Runtime[SupervisorRunContext],
    *,
    invoke_subagent: SubagentInvoker,
) -> dict[str, object]:
    """Invoke one bounded child and explicitly recover its terminal outcome."""

    if runtime.context is None:
        raise RuntimeError("supervisor run context is required")
    handoff = SupervisorHandoff.model_validate(state["active_handoff"])
    result = await invoke_subagent(handoff, state, runtime.context)
    dumped_result = _dump_value(result)
    common: dict[str, object] = {
        "active_handoff": None,
        "model_turns": state["model_turns"] + result.usage.model_turns,
        "subagent_results": [*state["subagent_results"], dumped_result],
    }
    if result.status == "completed":
        return {
            **common,
            "final_result": result.result,
            "events": _event(
                state,
                "subagent_completed",
                subagent_name=result.subagent_name,
            ),
        }
    if result.termination is None:
        raise AssertionError("terminated subagent result requires termination")
    return {
        **common,
        "termination": _supervisor_termination(
            state,
            status=result.status,
            reason=f"subagent_{result.status}:{result.termination.reason}",
            last_subagent=result.subagent_name,
        ),
        "events": _event(
            state,
            "subagent_recovered",
            subagent_name=result.subagent_name,
            status=result.status,
            reason=result.termination.reason,
        ),
    }


def _route_after_supervisor(state: SupervisorState) -> str:
    return END if state["termination"] is not None else "subagent"


def build_supervisor_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    router: SupervisorRouter | None = None,
    invoke_subagent: SubagentInvoker | None = None,
) -> CompiledStateGraph[SupervisorState, SupervisorRunContext, SupervisorState, SupervisorState]:
    """Compile the deterministic supervisor around two shared research graphs."""

    specs = _subagent_specs()
    graphs: Mapping[SubagentName, CompiledStateGraph[Any, Any, Any, Any]] = {
        name: build_research_graph(checkpointer=checkpointer, spec=spec)
        for name, spec in specs.items()
    }
    selected_invoker = invoke_subagent or partial(
        _invoke_research_subagent,
        graphs=graphs,
        specs=specs,
    )
    builder = StateGraph(SupervisorState, context_schema=SupervisorRunContext)
    builder.add_node(
        "route",
        cast(Any, partial(supervisor_route_node, router=router or SupervisorRouter())),
    )
    builder.add_node(
        "subagent",
        cast(Any, partial(supervisor_subagent_node, invoke_subagent=selected_invoker)),
    )
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", _route_after_supervisor)
    builder.add_edge("subagent", END)
    return builder.compile(checkpointer=checkpointer)
