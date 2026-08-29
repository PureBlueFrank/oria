"""Checkpointable state and per-invocation context for the research agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

from pydantic import Field

from oria.core.context import Context
from oria.core.types import JsonValue, Message, ValueModel
from oria.prompts import PromptManager


class ResearchLimits(ValueModel):
    max_model_turns: int = Field(default=6, ge=1)
    max_tool_calls: int = Field(default=4, ge=1)
    max_input_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int = Field(default=8_000, ge=1)
    max_total_tokens: int = Field(default=40_000, ge=1)
    max_cost: float = Field(default=5.0, ge=0)
    max_inline_tool_bytes: int = Field(default=32 * 1024, ge=256)
    max_validation_repairs: Literal[1] = 1
    no_progress_limit: Literal[2] = 2


@dataclass(frozen=True, slots=True)
class ResearchRunContext:
    """Trusted execution context supplied separately on every graph invocation."""

    ctx: Context
    limits: ResearchLimits = field(default_factory=ResearchLimits)
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.deadline_at is not None and (
            self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None
        ):
            raise ValueError("research deadline must include a timezone")


class ResearchState(TypedDict):
    user_request: str
    effective_at: str
    max_candidates: int
    messages: list[dict[str, JsonValue]]
    model_turns: int
    tool_calls_total: int
    validation_repairs: int
    seen_evidence_fingerprints: list[str]
    no_progress_streak: int
    pending_tool_calls: list[dict[str, JsonValue]]
    structured_output: dict[str, JsonValue] | None
    rule_result: dict[str, JsonValue] | None
    merchant_result: dict[str, JsonValue] | None
    proposal: dict[str, JsonValue] | None
    termination: dict[str, JsonValue] | None
    finalization_only: bool
    repair_pending: bool
    input_tokens: int
    output_tokens: int
    total_cost: float
    safe_evidence_refs: list[str]
    events: list[dict[str, JsonValue]]


def initial_research_state(
    *,
    user_request: str,
    effective_at: str,
    max_candidates: int = 10,
    prompts: PromptManager | None = None,
) -> ResearchState:
    """Build JSON-serializable state with an explicitly fixed prompt version."""

    if not user_request.strip():
        raise ValueError("user request must be non-empty")
    if max_candidates < 1 or max_candidates > 100:
        raise ValueError("max_candidates must be between 1 and 100")
    rendered = (prompts or PromptManager()).render(
        "merchant_selection",
        version=1,
        effective_at=effective_at,
        max_candidates=max_candidates,
    )
    messages = [
        Message(role="system", content=rendered).model_dump(mode="json"),
        Message(role="user", content=user_request).model_dump(mode="json"),
    ]
    return ResearchState(
        user_request=user_request,
        effective_at=effective_at,
        max_candidates=max_candidates,
        messages=messages,
        model_turns=0,
        tool_calls_total=0,
        validation_repairs=0,
        seen_evidence_fingerprints=[],
        no_progress_streak=0,
        pending_tool_calls=[],
        structured_output=None,
        rule_result=None,
        merchant_result=None,
        proposal=None,
        termination=None,
        finalization_only=False,
        repair_pending=False,
        input_tokens=0,
        output_tokens=0,
        total_cost=0.0,
        safe_evidence_refs=[],
        events=[],
    )
