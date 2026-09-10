"""Checkpoint-safe values and state for deterministic subagent supervision."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict, cast

from pydantic import Field, model_validator

from oria.agent.models import AgentTermination
from oria.core.types import JsonValue, ValueModel

DEFAULT_MAX_HANDOFFS = 2
SubagentName = Literal["campaign_research", "attribution_research"]


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
    events: NotRequired[list[dict[str, JsonValue]]]


def initial_supervisor_state(
    *,
    user_request: str,
    effective_at: str,
    max_candidates: int = 10,
    max_handoffs: int = DEFAULT_MAX_HANDOFFS,
) -> SupervisorState:
    """Build a fully defaulted JSON-serializable supervisor state."""

    if not user_request.strip():
        raise ValueError("user request must be non-empty")
    if not effective_at.strip():
        raise ValueError("effective at must be non-empty")
    if max_candidates < 1 or max_candidates > 100:
        raise ValueError("max candidates must be between 1 and 100")
    if max_handoffs < 1:
        raise ValueError("max handoffs must be positive")
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
            "events": [],
        },
    )
