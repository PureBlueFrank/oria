"""Fair single-agent versus multi-agent comparison contracts."""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, model_validator

from oria.agent import (
    ResearchLimits,
    ResearchRunContext,
    SupervisorRunContext,
    attribution_research_spec,
    build_research_graph,
    build_supervisor_graph,
    initial_attribution_state,
    initial_supervisor_state,
)
from oria.core.context import RuntimeServices
from oria.core.types import JsonValue, Principal, ValueModel
from oria.eval.attribution import (
    AttributionBlindItem,
    build_attribution_eval_runtime,
)
from oria.eval.datasets import AttributionGoldenCase
from oria.eval.nightly import NightlyBudget

Architecture = Literal["single", "multi"]


class ComparisonError(RuntimeError):
    """Raised when a comparison would violate its preregistered contract."""


class ComparisonBudget(NightlyBudget):
    """Identical aggregate and per-case limits applied to both architectures."""

    max_model_turns: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    per_case_timeout_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_token_total(self) -> Self:
        if self.max_total_tokens != self.max_input_tokens + self.max_output_tokens:
            raise ValueError("total-token budget must equal input plus output budgets")
        return self


class ArchitectureBudgets(ValueModel):
    """Explicit pair that rejects unequal allocation before a run starts."""

    single: ComparisonBudget
    multi: ComparisonBudget

    @model_validator(mode="after")
    def require_equal_budgets(self) -> Self:
        if self.single != self.multi:
            raise ValueError("single and multi architectures require identical total budgets")
        return self


class ComparisonConfig(ValueModel):
    """Frozen run configuration shared by the comparison harness."""

    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    seed: str = Field(min_length=1)
    repetitions: int = Field(gt=0)
    model_id: str = Field(default="attribution_replay_v1", min_length=1)
    tool_profile: str = Field(default="attribution_v2", min_length=1)
    termination_rule: str = Field(default="research_limits_v1", min_length=1)
    hide_architecture_labels: Literal[True] = True
    budgets: ArchitectureBudgets


class RubricMetric(ValueModel):
    """A preregistered metric and its direction of improvement."""

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category: Literal["quality", "cost", "latency", "variance"]
    higher_is_better: bool


class ComparisonRubric(ValueModel):
    """Hash-bound rubric and metric plan frozen before execution."""

    schema_version: Literal[1] = 1
    rubric_version: str = Field(min_length=1)
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_criteria: tuple[dict[str, JsonValue], ...]
    metrics: tuple[RubricMetric, ...]
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_metric_categories(self) -> Self:
        categories = {metric.category for metric in self.metrics}
        required = {"quality", "cost", "latency", "variance"}
        if categories != required:
            raise ValueError("comparison rubric requires quality, cost, latency, and variance")
        if len({metric.metric_id for metric in self.metrics}) != len(self.metrics):
            raise ValueError("comparison rubric metric ids must be unique")
        return self


class ArchitectureRunResult(ValueModel):
    """All observations for one architecture, case, and repetition."""

    architecture: Architecture
    case_id: str = Field(min_length=1)
    repetition: int = Field(gt=0)
    blind_item_id: str = Field(min_length=1)
    quality_score: float = Field(ge=0, le=1)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    termination_reason: str | None = None

    @model_validator(mode="after")
    def validate_total_tokens(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("run total tokens must equal input plus output tokens")
        return self


class ComparisonJudgePacket(ValueModel):
    """Architecture-neutral scoring input handed to a judge."""

    blind_item_id: str
    rubric_version: str
    criteria: tuple[dict[str, JsonValue], ...]
    response: AttributionBlindItem


class ComparisonExecutionSlot(ValueModel):
    """One replayable position in the randomized execution schedule."""

    position: int = Field(ge=0)
    architecture: Architecture
    case_id: str
    repetition: int = Field(gt=0)


class ArchitectureMetrics(ValueModel):
    """Aggregate observations without discarding any repeated run."""

    architecture: Architecture
    run_count: int = Field(gt=0)
    mean_quality: float = Field(ge=0, le=1)
    mean_tool_calls: float = Field(ge=0)
    mean_input_tokens: float = Field(ge=0)
    mean_output_tokens: float = Field(ge=0)
    mean_total_tokens: float = Field(ge=0)
    mean_cost_usd: float = Field(ge=0)
    mean_latency_ms: float = Field(ge=0)
    quality_variance: float = Field(ge=0)
    tool_calls_variance: float = Field(ge=0)
    total_tokens_variance: float = Field(ge=0)
    cost_variance: float = Field(ge=0)
    latency_variance: float = Field(ge=0)


class ComparisonDelta(ValueModel):
    """Multi minus single descriptive differences."""

    quality: float
    tool_calls: float
    total_tokens: float
    cost_usd: float
    latency_ms: float


class ComparisonReport(ValueModel):
    """Complete fair-comparison record; no best-run selection is represented."""

    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    verification_level: Literal["fixture"] = "fixture"
    status: Literal["descriptive_only"] = "descriptive_only"
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: ComparisonConfig
    rubric: ComparisonRubric
    execution_order: tuple[ComparisonExecutionSlot, ...]
    runs: tuple[ArchitectureRunResult, ...]
    single: ArchitectureMetrics
    multi: ArchitectureMetrics
    delta_multi_minus_single: ComparisonDelta
    conclusion: Literal["multi_improved", "multi_regressed", "mixed_or_equal"]
    applicability_boundary: str = Field(min_length=1)
    significance: Literal["descriptive_only"] = "descriptive_only"

    @model_validator(mode="after")
    def validate_complete_comparison(self) -> Self:
        architectures = {run.architecture for run in self.runs}
        if architectures != {"single", "multi"}:
            raise ValueError("comparison report requires runs from both architectures")
        if self.single.architecture != "single" or self.multi.architecture != "multi":
            raise ValueError("aggregate metrics must match their architecture fields")
        expected = self.config.repetitions * self.config.budgets.single.max_cases
        if self.single.run_count != expected or self.multi.run_count != expected:
            raise ValueError("comparison report cannot omit or select repeated runs")
        if len(self.runs) != expected * 2:
            raise ValueError("comparison report must retain every architecture run")
        return self


Judge = Callable[[ComparisonJudgePacket], Awaitable[float]]


def randomized_execution_order(
    cases: tuple[AttributionGoldenCase, ...],
    *,
    repetitions: int,
    seed: str,
) -> tuple[ComparisonExecutionSlot, ...]:
    """Build a deterministic, globally shuffled order containing every paired run."""

    if repetitions < 1:
        raise ComparisonError("comparison repetitions must be positive")
    pairs = [
        (architecture, case.case_id, repetition)
        for repetition in range(1, repetitions + 1)
        for case in cases
        for architecture in cast(tuple[Architecture, ...], ("single", "multi"))
    ]
    random.Random(seed).shuffle(pairs)
    return tuple(
        ComparisonExecutionSlot(
            position=position,
            architecture=architecture,
            case_id=case_id,
            repetition=repetition,
        )
        for position, (architecture, case_id, repetition) in enumerate(pairs)
    )


def _principal(tenant_id: str, *, kind: Literal["human", "service"]) -> Principal:
    return Principal(
        subject_id="compare-reviewer" if kind == "human" else "compare-runner",
        tenant_id=tenant_id,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="trusted-eval-fixture",
    )


def _research_limits(budget: ComparisonBudget) -> ResearchLimits:
    return ResearchLimits(
        max_model_turns=budget.max_model_turns,
        max_tool_calls=budget.max_tool_calls,
        max_input_tokens=budget.max_input_tokens,
        max_output_tokens=budget.max_output_tokens,
        max_total_tokens=budget.max_total_tokens,
        max_cost=budget.max_cost_usd,
    )


def _blind_item(
    state: dict[str, Any],
    case: AttributionGoldenCase,
    *,
    blind_item_id: str,
) -> AttributionBlindItem:
    conclusion = cast(dict[str, Any] | None, state.get("conclusion") or state.get("final_result"))
    if conclusion is None:
        return AttributionBlindItem(
            blind_case_id=blind_item_id,
            conversation_history=case.conversation_history,
            question=case.question,
            observed_outcome="runtime_failure",
            conclusion=None,
            hypotheses=(),
            evidence=(),
            requested_data=(),
        )
    return AttributionBlindItem(
        blind_case_id=blind_item_id,
        conversation_history=case.conversation_history,
        question=case.question,
        observed_outcome=cast(Any, conclusion["outcome"]),
        conclusion=cast(str | None, conclusion["conclusion"]),
        hypotheses=tuple(cast(list[dict[str, JsonValue]], conclusion["hypotheses"])),
        evidence=tuple(cast(list[dict[str, JsonValue]], conclusion["evidence"])),
        requested_data=tuple(cast(list[str], conclusion["requested_data"])),
    )


async def fixture_judge(packet: ComparisonJudgePacket) -> float:
    """Score replay output structure without access to architecture or Golden labels."""

    response = packet.response
    if response.observed_outcome == "runtime_failure":
        return 0.0
    if response.observed_outcome == "insufficient":
        return 1.0 if response.requested_data else 0.5
    score = 0.4
    score += 0.3 if response.hypotheses else 0.0
    score += 0.2 if response.evidence else 0.0
    score += 0.1 if response.observed_outcome in {"attributed", "conflicting"} else 0.0
    return score


async def run_architecture_slot(
    slot: ComparisonExecutionSlot,
    *,
    case: AttributionGoldenCase,
    base_runtime: RuntimeServices,
    query_database: Path,
    budget: ComparisonBudget,
    rubric: ComparisonRubric,
    judge: Judge = fixture_judge,
    clock: Callable[[], float] = time.monotonic,
) -> ArchitectureRunResult:
    """Run either graph through the same scoped fixture runtime and blind judge seam."""

    runtime = build_attribution_eval_runtime(base_runtime, (case,), query_database)
    actor = _principal(case.tenant_id, kind="human")
    executor = _principal(case.tenant_id, kind="service")
    ctx = runtime.new_context(
        actor=actor,
        executor=executor,
        session_id="comparison-fixture",
        thread_id=f"{case.case_id}-{slot.position}",
        run_id=case.case_id,
    )
    limits = _research_limits(budget)
    started = clock()
    deadline = datetime.now(UTC) + timedelta(seconds=budget.per_case_timeout_seconds)
    try:
        if slot.architecture == "single":
            state = await build_research_graph(
                checkpointer=InMemorySaver(), spec=attribution_research_spec()
            ).ainvoke(
                initial_attribution_state(
                    question=case.question,
                    analysis_period="2026-08-18/2026-09-01",
                    conversation_history=case.conversation_history,
                    tenant_id=case.tenant_id,
                ),
                config={"configurable": {"thread_id": f"single-{slot.position}"}},
                context=ResearchRunContext(ctx=ctx, limits=limits, deadline_at=deadline),
            )
            usage = state
        else:
            state = await build_supervisor_graph(checkpointer=InMemorySaver()).ainvoke(
                initial_supervisor_state(
                    user_request=case.question,
                    effective_at="2026-08-18/2026-09-01",
                ),
                config={"configurable": {"thread_id": f"multi-{slot.position}"}},
                context=SupervisorRunContext(ctx=ctx, attribution_limits=limits),
            )
            subagent_results = cast(list[dict[str, Any]], state.get("subagent_results", []))
            usage = (
                cast(dict[str, Any], subagent_results[-1].get("usage", {}))
                if subagent_results
                else {}
            )
        elapsed_ms = (clock() - started) * 1000
        blind_hash = hashlib.sha256(
            f"{rubric.preregistration_sha256}:{slot.position}:{case.case_id}".encode()
        ).hexdigest()[:16]
        blind_item_id = f"blind_{blind_hash}"
        response = _blind_item(cast(dict[str, Any], state), case, blind_item_id=blind_item_id)
        packet = ComparisonJudgePacket(
            blind_item_id=blind_item_id,
            rubric_version=rubric.rubric_version,
            criteria=rubric.judge_criteria,
            response=response,
        )
        quality = await judge(packet)
        input_tokens = cast(int, usage.get("input_tokens", 0))
        output_tokens = cast(int, usage.get("output_tokens", 0))
        termination = cast(dict[str, Any] | None, state.get("termination"))
        return ArchitectureRunResult(
            architecture=slot.architecture,
            case_id=case.case_id,
            repetition=slot.repetition,
            blind_item_id=blind_item_id,
            quality_score=quality,
            tool_calls=cast(int, usage.get("tool_calls_total", 0)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cast(float, usage.get("total_cost", 0.0)),
            latency_ms=elapsed_ms,
            termination_reason=(
                None if termination is None else cast(str, termination.get("reason"))
            ),
        )
    finally:
        await runtime.aclose()
