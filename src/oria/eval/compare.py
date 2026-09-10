"""Fair single-agent versus multi-agent comparison contracts."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

import yaml
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
from oria.config import resolve_runtime_config
from oria.core.context import RuntimeServices
from oria.core.protocols import LLMProvider
from oria.core.runtime import build_runtime
from oria.core.types import JsonValue, Principal, ValueModel
from oria.data import initialize_data
from oria.eval.attribution import (
    AttributionBlindItem,
    build_attribution_eval_runtime,
    load_attribution_rubric,
)
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset
from oria.eval.nightly import (
    NightlyBudget,
    NightlyBudgetExceeded,
    NightlyBudgetLedger,
    PricingSnapshot,
    TieredModelPrices,
    TokenPrices,
)

Architecture = Literal["single", "multi"]


class ComparisonError(RuntimeError):
    """Raised when a comparison would violate its preregistered contract."""


class ComparisonBudget(NightlyBudget):
    """Identical aggregate and per-case limits applied to both architectures."""

    max_model_turns: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    per_case_max_model_turns: int = Field(gt=0)
    per_case_max_tool_calls: int = Field(gt=0)
    per_case_max_input_tokens: int = Field(gt=0)
    per_case_max_output_tokens: int = Field(gt=0)
    per_case_max_cost_usd: float = Field(gt=0)
    per_case_timeout_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_token_total(self) -> Self:
        if self.max_total_tokens != self.max_input_tokens + self.max_output_tokens:
            raise ValueError("total-token budget must equal input plus output budgets")
        if self.max_model_turns < self.max_cases * self.per_case_max_model_turns:
            raise ValueError("model-turn budget cannot cover declared case runs")
        if self.max_tool_calls < self.max_cases * self.per_case_max_tool_calls:
            raise ValueError("tool-call budget cannot cover declared case runs")
        if self.max_input_tokens < self.max_cases * self.per_case_max_input_tokens:
            raise ValueError("input-token budget cannot cover declared case runs")
        if self.max_output_tokens < self.max_cases * self.per_case_max_output_tokens:
            raise ValueError("output-token budget cannot cover declared case runs")
        if self.max_cost_usd < self.max_cases * self.per_case_max_cost_usd:
            raise ValueError("cost budget cannot cover declared case runs")
        return self


class ComparisonLiveBudget(NightlyBudget):
    """One architecture's aggregate and per-case Live limits."""

    max_model_requests: int = Field(gt=0)
    per_case_max_model_turns: int = Field(gt=0)
    per_case_max_tool_calls: int = Field(gt=0)
    per_case_max_input_tokens: int = Field(gt=0)
    per_case_max_output_tokens: int = Field(gt=0)
    per_case_max_cost_usd: float = Field(gt=0)
    per_case_timeout_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_aggregate_bounds(self) -> Self:
        if self.max_model_requests < self.max_cases * self.per_case_max_model_turns:
            raise ValueError("Live model-request budget cannot cover declared case runs")
        if self.max_input_tokens < self.max_cases * self.per_case_max_input_tokens:
            raise ValueError("Live input-token budget cannot cover worst-case reservations")
        if self.max_output_tokens < self.max_cases * self.per_case_max_output_tokens:
            raise ValueError("Live output-token budget cannot cover worst-case reservations")
        if self.max_cost_usd < self.max_cases * self.per_case_max_cost_usd:
            raise ValueError("Live cost budget cannot cover worst-case reservations")
        return self


class ComparisonLiveTarget(ValueModel):
    """Explicit provider target for a frozen single/multi Live comparison."""

    target_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    provider: Literal["deepseek", "codex"]
    model: str = Field(min_length=1)
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    pricing_snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,127}$")
    rate_tier: Literal["peak", "off_peak"]
    repetitions: int = Field(gt=0)
    order_seed: str = Field(min_length=1)
    budget: ComparisonLiveBudget


class ComparisonLiveConfig(ValueModel):
    """Configured Live targets available to the comparison runner."""

    schema_version: Literal[1] = 1
    recommended_target: str | None = None
    targets: tuple[ComparisonLiveTarget, ...]

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        target_ids = {target.target_id for target in self.targets}
        if not self.targets or len(target_ids) != len(self.targets):
            raise ValueError("comparison Live targets must be non-empty and unique")
        if self.recommended_target is not None and self.recommended_target not in target_ids:
            raise ValueError("recommended comparison Live target must be configured")
        return self


class ComparisonLivePreflight(ValueModel):
    """Request-free validation result for one comparison Live target."""

    schema_version: Literal[1] = 1
    suite: Literal["attribution_comparison"] = "attribution_comparison"
    target_id: str
    status: Literal["ready", "blocked"]
    request_count: Literal[0] = 0
    checked_at: datetime
    reason: str | None = None
    dataset_version: str | None = None
    dataset_sha256: str | None = None
    holdout_case_count: int | None = Field(default=None, ge=1)
    repetitions: int | None = Field(default=None, ge=1)
    expected_architecture_runs: int | None = Field(default=None, ge=2)
    pricing_snapshot_id: str | None = None


class ArchitectureBudgets(ValueModel):
    """Explicit pair that rejects unequal allocation before a run starts."""

    single: ComparisonBudget | ComparisonLiveBudget
    multi: ComparisonBudget | ComparisonLiveBudget

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


class ComparisonFixturePlan(ValueModel):
    """Offline budget and synthetic token prices loaded by the CLI."""

    schema_version: Literal[1] = 1
    pricing_snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,127}$")
    budget: ComparisonBudget
    token_prices: TokenPrices


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
    model_turns: int = Field(ge=0)
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
    mean_model_turns: float = Field(ge=0)
    mean_tool_calls: float = Field(ge=0)
    mean_input_tokens: float = Field(ge=0)
    mean_output_tokens: float = Field(ge=0)
    mean_total_tokens: float = Field(ge=0)
    mean_cost_usd: float = Field(ge=0)
    mean_latency_ms: float = Field(ge=0)
    quality_variance: float = Field(ge=0)
    model_turns_variance: float = Field(ge=0)
    tool_calls_variance: float = Field(ge=0)
    total_tokens_variance: float = Field(ge=0)
    cost_variance: float = Field(ge=0)
    latency_variance: float = Field(ge=0)


class ComparisonDelta(ValueModel):
    """Multi minus single descriptive differences."""

    quality: float
    model_turns: float
    tool_calls: float
    total_tokens: float
    cost_usd: float
    latency_ms: float


class ComparisonReport(ValueModel):
    """Complete fair-comparison record; no best-run selection is represented."""

    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    verification_level: Literal["fixture", "live"] = "fixture"
    run_status: Literal["completed", "in_progress"] = "completed"
    target_id: str | None = None
    status: Literal["descriptive_only"] = "descriptive_only"
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_snapshot_id: str = Field(min_length=1)
    config: ComparisonConfig
    rubric: ComparisonRubric
    execution_order: tuple[ComparisonExecutionSlot, ...]
    runs: tuple[ArchitectureRunResult, ...]
    single: ArchitectureMetrics | None
    multi: ArchitectureMetrics | None
    delta_multi_minus_single: ComparisonDelta | None
    conclusion: Literal["multi_improved", "multi_regressed", "mixed_or_equal"] | None
    applicability_boundary: str = Field(min_length=1)
    significance: Literal["descriptive_only"] = "descriptive_only"

    @model_validator(mode="after")
    def validate_complete_comparison(self) -> Self:
        if self.verification_level == "live" and self.target_id is None:
            raise ValueError("Live comparison report requires an explicit target")
        if self.run_status == "in_progress":
            return self
        architectures = {run.architecture for run in self.runs}
        if architectures != {"single", "multi"}:
            raise ValueError("comparison report requires runs from both architectures")
        if self.single is None or self.multi is None:
            raise ValueError("completed comparison requires aggregate metrics")
        if self.single.architecture != "single" or self.multi.architecture != "multi":
            raise ValueError("aggregate metrics must match their architecture fields")
        if self.delta_multi_minus_single is None or self.conclusion is None:
            raise ValueError("completed comparison requires a descriptive conclusion")
        expected = self.config.budgets.single.max_cases
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


def _research_limits(budget: ComparisonBudget | ComparisonLiveBudget) -> ResearchLimits:
    return ResearchLimits(
        max_model_turns=budget.per_case_max_model_turns,
        max_tool_calls=budget.per_case_max_tool_calls,
        max_input_tokens=budget.per_case_max_input_tokens,
        max_output_tokens=budget.per_case_max_output_tokens,
        max_total_tokens=budget.per_case_max_input_tokens + budget.per_case_max_output_tokens,
        max_cost=budget.per_case_max_cost_usd,
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
    answered = response.observed_outcome in {"attributed", "conflicting"}
    criterion_scores = {
        "outcome_classification": 2,
        "evidence_support": 2
        if (answered and response.evidence) or (not answered and response.requested_data)
        else 0,
        "evidence_chain_sufficiency": 2
        if (answered and response.hypotheses and response.evidence)
        or (not answered and response.requested_data)
        else 0,
        "abstention_and_conflict": 2
        if (response.observed_outcome == "insufficient" and response.requested_data)
        or (response.observed_outcome == "conflicting" and len(response.hypotheses) >= 2)
        or (response.observed_outcome == "attributed" and response.conclusion is not None)
        else 0,
        "safety_and_scope": 2,
    }
    weighted = 0.0
    for criterion in packet.criteria:
        criterion_id = criterion.get("id")
        weight = criterion.get("weight")
        if not isinstance(criterion_id, str) or not isinstance(weight, (int, float)):
            raise ComparisonError("blind judge received an invalid preregistered criterion")
        weighted += criterion_scores.get(criterion_id, 0) * float(weight)
    return weighted / 2


async def run_architecture_slot(
    slot: ComparisonExecutionSlot,
    *,
    case: AttributionGoldenCase,
    base_runtime: RuntimeServices,
    query_database: Path,
    budget: ComparisonBudget | ComparisonLiveBudget,
    rubric: ComparisonRubric,
    llm: LLMProvider | None = None,
    judge: Judge = fixture_judge,
    clock: Callable[[], float] = time.monotonic,
) -> ArchitectureRunResult:
    """Run either graph through the same scoped fixture runtime and blind judge seam."""

    actor = _principal(case.tenant_id, kind="human")
    executor = _principal(case.tenant_id, kind="service")
    runtime = build_attribution_eval_runtime(
        base_runtime,
        (case,),
        query_database,
        llm=llm,
        trusted_actors=(actor,),
        trusted_executors=(executor,),
    )
    ctx = runtime.new_context(
        actor=actor,
        executor=executor,
        session_id="comparison-live" if llm is not None else "comparison-fixture",
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
                    conversation_history=case.conversation_history,
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
            model_turns=cast(int, usage.get("model_turns", state.get("model_turns", 0))),
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


def preregister_comparison_rubric(rubric_path: Path) -> ComparisonRubric:
    """Bind the reviewed rubric and comparison metric plan before execution."""

    rubric = load_attribution_rubric(rubric_path)
    try:
        payload = rubric_path.read_bytes()
    except OSError as exc:
        raise ComparisonError("comparison rubric is unavailable") from exc
    rubric_sha256 = hashlib.sha256(payload).hexdigest()
    metrics = (
        RubricMetric(metric_id="quality", category="quality", higher_is_better=True),
        RubricMetric(metric_id="cost_usd", category="cost", higher_is_better=False),
        RubricMetric(metric_id="latency_ms", category="latency", higher_is_better=False),
        RubricMetric(metric_id="repetition_variance", category="variance", higher_is_better=False),
    )
    criteria = tuple(
        cast(dict[str, JsonValue], criterion.model_dump(mode="json"))
        for criterion in rubric.criteria
    )
    frozen = {
        "rubric_version": rubric.rubric_version,
        "rubric_sha256": rubric_sha256,
        "judge_criteria": criteria,
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
    }
    digest = hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ComparisonRubric(
        rubric_version=rubric.rubric_version,
        rubric_sha256=rubric_sha256,
        judge_criteria=criteria,
        metrics=metrics,
        preregistration_sha256=digest,
    )


def assert_preregistered_rubric(rubric_path: Path, registered: ComparisonRubric) -> None:
    """Reject rubric replacement or metric-plan mutation after preregistration."""

    current = preregister_comparison_rubric(rubric_path)
    if current != registered:
        raise ComparisonError("comparison rubric changed after preregistration")


def _variance(values: list[float]) -> float:
    return statistics.variance(values) if len(values) > 1 else 0.0


def _architecture_metrics(
    architecture: Architecture, runs: tuple[ArchitectureRunResult, ...]
) -> ArchitectureMetrics:
    selected = [run for run in runs if run.architecture == architecture]
    if not selected:
        raise ComparisonError(f"comparison has no {architecture} runs")

    def values(name: str) -> list[float]:
        return [float(getattr(run, name)) for run in selected]

    def repetition_means(name: str) -> list[float]:
        repetitions = sorted({run.repetition for run in selected})
        return [
            statistics.fmean(
                float(getattr(run, name)) for run in selected if run.repetition == repetition
            )
            for repetition in repetitions
        ]

    count = len(selected)
    return ArchitectureMetrics(
        architecture=architecture,
        run_count=count,
        mean_quality=statistics.fmean(values("quality_score")),
        mean_model_turns=statistics.fmean(values("model_turns")),
        mean_tool_calls=statistics.fmean(values("tool_calls")),
        mean_input_tokens=statistics.fmean(values("input_tokens")),
        mean_output_tokens=statistics.fmean(values("output_tokens")),
        mean_total_tokens=statistics.fmean(values("total_tokens")),
        mean_cost_usd=statistics.fmean(values("cost_usd")),
        mean_latency_ms=statistics.fmean(values("latency_ms")),
        quality_variance=_variance(repetition_means("quality_score")),
        model_turns_variance=_variance(repetition_means("model_turns")),
        tool_calls_variance=_variance(repetition_means("tool_calls")),
        total_tokens_variance=_variance(repetition_means("total_tokens")),
        cost_variance=_variance(repetition_means("cost_usd")),
        latency_variance=_variance(repetition_means("latency_ms")),
    )


def _price_run(run: ArchitectureRunResult, prices: TokenPrices) -> ArchitectureRunResult:
    cost = (
        run.input_tokens * prices.input_cache_miss_per_million_usd
        + run.output_tokens * max(prices.output_per_million_usd, prices.reasoning_per_million_usd)
    ) / 1_000_000
    return run.model_copy(update={"cost_usd": cost})


def _assert_architecture_budget(
    architecture: Architecture,
    runs: tuple[ArchitectureRunResult, ...],
    budget: ComparisonBudget,
) -> None:
    selected = [run for run in runs if run.architecture == architecture]
    totals = {
        "cases": len(selected),
        "model turns": sum(run.model_turns for run in selected),
        "tool calls": sum(run.tool_calls for run in selected),
        "input tokens": sum(run.input_tokens for run in selected),
        "output tokens": sum(run.output_tokens for run in selected),
        "total tokens": sum(run.total_tokens for run in selected),
        "cost": sum(run.cost_usd for run in selected),
        "wall seconds": sum(run.latency_ms for run in selected) / 1000,
    }
    maxima = {
        "cases": budget.max_cases,
        "model turns": budget.max_model_turns,
        "tool calls": budget.max_tool_calls,
        "input tokens": budget.max_input_tokens,
        "output tokens": budget.max_output_tokens,
        "total tokens": budget.max_total_tokens,
        "cost": budget.max_cost_usd,
        "wall seconds": budget.max_wall_seconds,
    }
    exceeded = [name for name, total in totals.items() if total > maxima[name]]
    if exceeded:
        raise ComparisonError(
            f"{architecture} architecture exceeded comparison budget: {', '.join(exceeded)}"
        )


def _pricing_for(
    snapshot: PricingSnapshot, *, model_id: str, rate_tier: Literal["peak", "off_peak"]
) -> TokenPrices:
    try:
        tiers = snapshot.models[model_id]
    except KeyError as exc:
        raise ComparisonError("comparison model is absent from pricing snapshot") from exc
    return tiers.peak if rate_tier == "peak" else tiers.off_peak


def load_comparison_fixture_plan(path: Path) -> ComparisonFixturePlan:
    """Load a strict offline comparison budget without contacting a provider."""

    try:
        return ComparisonFixturePlan.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ComparisonError("comparison budget is unavailable or invalid") from exc


def load_comparison_live_config(path: Path) -> ComparisonLiveConfig:
    """Load the strict list of explicitly selectable comparison Live targets."""

    try:
        return ComparisonLiveConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ComparisonError("comparison Live configuration is unavailable or invalid") from exc


def load_comparison_pricing_snapshot(path: Path) -> PricingSnapshot:
    """Load a pricing snapshot used to reserve Live comparison requests."""

    try:
        return PricingSnapshot.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ComparisonError("comparison pricing snapshot is unavailable or invalid") from exc


def select_comparison_live_target(
    config: ComparisonLiveConfig, target_id: str
) -> ComparisonLiveTarget:
    """Resolve one configured Live target without falling back to a default."""

    target = next((item for item in config.targets if item.target_id == target_id), None)
    if target is None:
        raise ComparisonError("comparison Live target is not configured")
    return target


def preflight_comparison_live(
    *,
    config_path: Path,
    manifest_path: Path,
    rubric_path: Path,
    pricing_dir: Path,
    target_id: str,
    environ: Mapping[str, str],
    now: datetime,
    known_targets: frozenset[str],
) -> ComparisonLivePreflight:
    """Validate frozen assets, prices, target, and credential without creating a provider."""

    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ComparisonError("comparison Live preflight time must include a timezone")
        if target_id not in known_targets:
            raise ComparisonError("comparison Live target is unknown")
        target = select_comparison_live_target(load_comparison_live_config(config_path), target_id)
        dataset = load_golden_dataset(manifest_path)
        cases = tuple(
            case
            for case in dataset.cases
            if isinstance(case, AttributionGoldenCase) and case.split == "holdout"
        )
        if (
            dataset.manifest.review_status != "approved"
            or not dataset.manifest.human_review_complete
            or not dataset.manifest.holdout_frozen
            or dataset.manifest.holdout_case_count != len(cases)
        ):
            raise ComparisonError("comparison Live dataset is not reviewed and frozen")
        if len(cases) * target.repetitions != target.budget.max_cases:
            raise ComparisonError("comparison Live budget does not cover holdout repetitions")
        rubric = preregister_comparison_rubric(rubric_path)
        if rubric.rubric_sha256 != dataset.manifest.rubric_sha256:
            raise ComparisonError("comparison rubric does not match the frozen dataset")
        snapshot = load_comparison_pricing_snapshot(
            pricing_dir / f"{target.pricing_snapshot_id}.yaml"
        )
        if snapshot.snapshot_id != target.pricing_snapshot_id:
            raise ComparisonError("comparison pricing snapshot identity does not match target")
        if now > snapshot.valid_until:
            raise ComparisonError("comparison pricing snapshot is expired")
        if target.model not in snapshot.models:
            raise ComparisonError("comparison pricing snapshot does not cover target model")
        if not environ.get(target.credential_env, "").strip():
            return ComparisonLivePreflight(
                target_id=target_id,
                status="blocked",
                checked_at=now,
                reason="comparison Live credential is missing",
                dataset_version=dataset.manifest.dataset_version,
                dataset_sha256=dataset.manifest.dataset_sha256,
                holdout_case_count=len(cases),
                repetitions=target.repetitions,
                expected_architecture_runs=target.budget.max_cases * 2,
                pricing_snapshot_id=snapshot.snapshot_id,
            )
    except (ComparisonError, ValueError) as exc:
        return ComparisonLivePreflight(
            target_id=target_id,
            status="blocked",
            checked_at=now,
            reason=str(exc),
        )
    return ComparisonLivePreflight(
        target_id=target_id,
        status="ready",
        checked_at=now,
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        holdout_case_count=len(cases),
        repetitions=target.repetitions,
        expected_architecture_runs=target.budget.max_cases * 2,
        pricing_snapshot_id=snapshot.snapshot_id,
    )


def fixture_pricing_snapshot(plan: ComparisonFixturePlan) -> PricingSnapshot:
    """Wrap synthetic Fixture prices in the shared immutable pricing contract."""

    return PricingSnapshot(
        snapshot_id=plan.pricing_snapshot_id,
        currency="USD",
        unit="per_million_tokens",
        source_url="https://oria.invalid/eval/fixture-pricing",
        verified_at=datetime(2026, 9, 10, tzinfo=UTC),
        valid_until=datetime(2036, 9, 10, tzinfo=UTC),
        models={
            "attribution_replay_v1": TieredModelPrices(
                peak=plan.token_prices,
                off_peak=plan.token_prices,
            )
        },
    )


def _descriptive_summary(
    single: ArchitectureMetrics,
    multi: ArchitectureMetrics,
    *,
    verification_level: Literal["fixture", "live"],
) -> tuple[
    ComparisonDelta,
    Literal["multi_improved", "multi_regressed", "mixed_or_equal"],
    str,
]:
    delta = ComparisonDelta(
        quality=multi.mean_quality - single.mean_quality,
        model_turns=multi.mean_model_turns - single.mean_model_turns,
        tool_calls=multi.mean_tool_calls - single.mean_tool_calls,
        total_tokens=multi.mean_total_tokens - single.mean_total_tokens,
        cost_usd=multi.mean_cost_usd - single.mean_cost_usd,
        latency_ms=multi.mean_latency_ms - single.mean_latency_ms,
    )
    label = "Live" if verification_level == "live" else "Fixture"
    if delta.quality > 0:
        conclusion: Literal["multi_improved", "multi_regressed", "mixed_or_equal"] = (
            "multi_improved"
        )
        boundary = f"{label} descriptive quality gain; no statistical significance is claimed."
    elif delta.quality < 0:
        conclusion = "multi_regressed"
        boundary = f"Multi regressed in the frozen {label} comparison."
    else:
        conclusion = "mixed_or_equal"
        boundary = f"No {label} quality gain; supervisor routing value is operational only."
    return delta, conclusion, boundary


async def run_comparison(
    manifest_path: Path,
    *,
    rubric_path: Path,
    data_dir: Path,
    config: ComparisonConfig,
    pricing_snapshot: PricingSnapshot,
    rate_tier: Literal["peak", "off_peak"] = "peak",
    judge: Judge = fixture_judge,
) -> ComparisonReport:
    """Run every frozen case/repetition for both architectures and report all results."""

    dataset = load_golden_dataset(manifest_path)
    cases = tuple(case for case in dataset.cases if isinstance(case, AttributionGoldenCase))
    expected_runs = len(cases) * config.repetitions
    if config.budgets.single != config.budgets.multi:
        raise ComparisonError("single and multi architectures require identical total budgets")
    if config.budgets.single.max_cases != expected_runs:
        raise ComparisonError("comparison case budget must exactly cover the frozen dataset")
    if dataset.manifest.rubric_sha256 is None:
        raise ComparisonError("comparison dataset has no frozen rubric")
    rubric = preregister_comparison_rubric(rubric_path)
    if rubric.rubric_sha256 != dataset.manifest.rubric_sha256:
        raise ComparisonError("comparison rubric does not match the frozen dataset")
    prices = _pricing_for(pricing_snapshot, model_id=config.model_id, rate_tier=rate_tier)
    order = randomized_execution_order(cases, repetitions=config.repetitions, seed=config.seed)
    query_databases: dict[str, Path] = {}
    for variant in sorted({case.fixture_variant for case in cases}):
        query_database = data_dir / "fixtures" / variant / "analytics.db"
        generate_attribution_fixture(
            query_database,
            data_dir / "evaluation-only" / variant / "labels.db",
            fixture_variant=variant,
        )
        query_databases[variant] = query_database
    runtime_config = resolve_runtime_config(
        environ={"ORIA_ENVIRONMENT": "test"}, data_dir=data_dir / "runtime"
    )
    await initialize_data(runtime_config)
    base = await build_runtime(runtime_config)
    case_by_id = {case.case_id: case for case in cases}
    results: list[ArchitectureRunResult] = []
    try:
        for slot in order:
            case = case_by_id[slot.case_id]
            result = await run_architecture_slot(
                slot,
                case=case,
                base_runtime=base,
                query_database=query_databases[case.fixture_variant],
                budget=config.budgets.single,
                rubric=rubric,
                judge=judge,
            )
            results.append(_price_run(result, prices))
    finally:
        await base.aclose()
    assert_preregistered_rubric(rubric_path, rubric)
    all_runs = tuple(results)
    _assert_architecture_budget("single", all_runs, cast(ComparisonBudget, config.budgets.single))
    _assert_architecture_budget("multi", all_runs, cast(ComparisonBudget, config.budgets.multi))
    single = _architecture_metrics("single", all_runs)
    multi = _architecture_metrics("multi", all_runs)
    delta, conclusion, boundary = _descriptive_summary(single, multi, verification_level="fixture")
    return ComparisonReport(
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        pricing_snapshot_id=pricing_snapshot.snapshot_id,
        config=config,
        rubric=rubric,
        execution_order=order,
        runs=all_runs,
        single=single,
        multi=multi,
        delta_multi_minus_single=delta,
        conclusion=conclusion,
        applicability_boundary=boundary,
    )


def _validated_resume_runs(
    *,
    resume_runs: tuple[ArchitectureRunResult, ...],
    order: tuple[ComparisonExecutionSlot, ...],
) -> tuple[ArchitectureRunResult, ...]:
    allowed = {(slot.architecture, slot.case_id, slot.repetition) for slot in order}
    observed: set[tuple[Architecture, str, int]] = set()
    for run in resume_runs:
        key = (run.architecture, run.case_id, run.repetition)
        if key not in allowed or key in observed:
            raise ComparisonError("comparison Live resume records do not match the frozen plan")
        observed.add(key)
    return resume_runs


async def run_comparison_live(
    manifest_path: Path,
    *,
    rubric_path: Path,
    base_runtime: RuntimeServices,
    target: ComparisonLiveTarget,
    data_dir: Path,
    pricing_snapshot: PricingSnapshot,
    resume_runs: tuple[ArchitectureRunResult, ...] = (),
    max_new_case_runs: int | None = None,
    judge: Judge = fixture_judge,
) -> ComparisonReport:
    """Run or resume the frozen holdout with one real LLM and equal per-architecture ledgers."""

    if base_runtime.llm is None:
        raise ComparisonError("selected comparison Live provider is unavailable")
    if (
        base_runtime.config.llm.provider != target.provider
        or base_runtime.config.llm.model != target.model
    ):
        raise ComparisonError("runtime provider/model does not match comparison Live target")
    if pricing_snapshot.snapshot_id != target.pricing_snapshot_id:
        raise ComparisonError("comparison pricing snapshot identity does not match target")
    if datetime.now().astimezone() > pricing_snapshot.valid_until:
        raise ComparisonError("comparison pricing snapshot is expired")
    if max_new_case_runs is not None and max_new_case_runs < 1:
        raise ComparisonError("staged comparison case-run limit must be positive")

    dataset = load_golden_dataset(manifest_path)
    cases = tuple(
        case
        for case in dataset.cases
        if isinstance(case, AttributionGoldenCase) and case.split == "holdout"
    )
    expected_per_architecture = len(cases) * target.repetitions
    if not cases or expected_per_architecture != target.budget.max_cases:
        raise ComparisonError("comparison Live budget must exactly cover holdout repetitions")
    if (
        dataset.manifest.review_status != "approved"
        or not dataset.manifest.human_review_complete
        or not dataset.manifest.holdout_frozen
    ):
        raise ComparisonError("comparison Live dataset is not reviewed and frozen")
    if dataset.manifest.holdout_case_count != len(cases):
        raise ComparisonError("comparison Live holdout count does not match its manifest")
    if dataset.manifest.rubric_sha256 is None:
        raise ComparisonError("comparison dataset has no frozen rubric")
    rubric = preregister_comparison_rubric(rubric_path)
    if rubric.rubric_sha256 != dataset.manifest.rubric_sha256:
        raise ComparisonError("comparison rubric does not match the frozen dataset")
    prices = _pricing_for(pricing_snapshot, model_id=target.model, rate_tier=target.rate_tier)
    budgets = ArchitectureBudgets(single=target.budget, multi=target.budget)
    config = ComparisonConfig(
        seed=target.order_seed,
        repetitions=target.repetitions,
        model_id=target.model,
        budgets=budgets,
    )
    order = randomized_execution_order(
        cases, repetitions=target.repetitions, seed=target.order_seed
    )
    validated_resume = _validated_resume_runs(resume_runs=resume_runs, order=order)
    ledgers = {
        architecture: NightlyBudgetLedger(target.budget, prices)
        for architecture in cast(tuple[Architecture, ...], ("single", "multi"))
    }
    model_requests: dict[Architecture, int] = {"single": 0, "multi": 0}
    for run in validated_resume:
        try:
            reservation = ledgers[run.architecture].reserve(
                input_tokens=target.budget.per_case_max_input_tokens,
                max_output_tokens=target.budget.per_case_max_output_tokens,
            )
            ledgers[run.architecture].settle(
                reservation,
                cache_hit_tokens=0,
                cache_miss_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                reasoning_tokens=0,
            )
        except NightlyBudgetExceeded as exc:
            raise ComparisonError(
                "comparison Live resume records exceed an architecture budget"
            ) from exc
        model_requests[run.architecture] += run.model_turns
        if model_requests[run.architecture] > target.budget.max_model_requests:
            raise ComparisonError("comparison Live resume records exceed the model-request budget")

    query_databases: dict[str, Path] = {}
    for variant in sorted({case.fixture_variant for case in cases}):
        query_database = data_dir / "fixtures" / variant / "analytics.db"
        generate_attribution_fixture(
            query_database,
            data_dir / "evaluation-only" / variant / "labels.db",
            fixture_variant=variant,
        )
        query_databases[variant] = query_database

    case_by_id = {case.case_id: case for case in cases}
    completed_keys = {(run.architecture, run.case_id, run.repetition) for run in validated_resume}
    results = list(validated_resume)
    new_case_runs = 0
    for slot in order:
        key = (slot.architecture, slot.case_id, slot.repetition)
        if key in completed_keys:
            continue
        if max_new_case_runs is not None and new_case_runs >= max_new_case_runs:
            break
        if (
            model_requests[slot.architecture] + target.budget.per_case_max_model_turns
            > target.budget.max_model_requests
        ):
            raise ComparisonError("comparison Live model-request budget is exhausted")
        ledger = ledgers[slot.architecture]
        try:
            reservation = ledger.reserve(
                input_tokens=target.budget.per_case_max_input_tokens,
                max_output_tokens=target.budget.per_case_max_output_tokens,
            )
        except NightlyBudgetExceeded as exc:
            raise ComparisonError(str(exc)) from exc
        try:
            result = await run_architecture_slot(
                slot,
                case=case_by_id[slot.case_id],
                base_runtime=base_runtime,
                query_database=query_databases[case_by_id[slot.case_id].fixture_variant],
                budget=target.budget,
                rubric=rubric,
                llm=base_runtime.llm,
                judge=judge,
            )
            priced = _price_run(result, prices)
            ledger.settle(
                reservation,
                cache_hit_tokens=0,
                cache_miss_tokens=priced.input_tokens,
                output_tokens=priced.output_tokens,
                reasoning_tokens=0,
            )
        except NightlyBudgetExceeded as exc:
            raise ComparisonError(str(exc)) from exc
        except Exception:
            ledger.cancel(reservation)
            raise
        results.append(priced)
        completed_keys.add(key)
        model_requests[slot.architecture] += priced.model_turns
        if model_requests[slot.architecture] > target.budget.max_model_requests:
            raise ComparisonError("comparison Live model-request budget is exhausted")
        new_case_runs += 1

    assert_preregistered_rubric(rubric_path, rubric)
    all_runs = tuple(results)
    complete = len(all_runs) == len(order)
    architectures = {run.architecture for run in all_runs}
    single = _architecture_metrics("single", all_runs) if "single" in architectures else None
    multi = _architecture_metrics("multi", all_runs) if "multi" in architectures else None
    delta: ComparisonDelta | None = None
    conclusion: Literal["multi_improved", "multi_regressed", "mixed_or_equal"] | None = None
    boundary = "Staged Live comparison is incomplete; no architecture conclusion is available."
    if complete:
        if single is None or multi is None:
            raise ComparisonError("completed Live comparison is missing an architecture")
        delta, conclusion, boundary = _descriptive_summary(single, multi, verification_level="live")
    return ComparisonReport(
        verification_level="live",
        run_status="completed" if complete else "in_progress",
        target_id=target.target_id,
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        pricing_snapshot_id=pricing_snapshot.snapshot_id,
        config=config,
        rubric=rubric,
        execution_order=order,
        runs=all_runs,
        single=single,
        multi=multi,
        delta_multi_minus_single=delta,
        conclusion=conclusion,
        applicability_boundary=boundary,
    )
