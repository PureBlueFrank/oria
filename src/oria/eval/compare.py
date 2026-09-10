"""Fair single-agent versus multi-agent comparison contracts."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
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
from oria.config import resolve_runtime_config
from oria.core.context import RuntimeServices
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
from oria.eval.nightly import NightlyBudget, PricingSnapshot, TokenPrices

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
    verification_level: Literal["fixture"] = "fixture"
    status: Literal["descriptive_only"] = "descriptive_only"
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_snapshot_id: str = Field(min_length=1)
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
    _assert_architecture_budget("single", all_runs, config.budgets.single)
    _assert_architecture_budget("multi", all_runs, config.budgets.multi)
    single = _architecture_metrics("single", all_runs)
    multi = _architecture_metrics("multi", all_runs)
    delta = ComparisonDelta(
        quality=multi.mean_quality - single.mean_quality,
        model_turns=multi.mean_model_turns - single.mean_model_turns,
        tool_calls=multi.mean_tool_calls - single.mean_tool_calls,
        total_tokens=multi.mean_total_tokens - single.mean_total_tokens,
        cost_usd=multi.mean_cost_usd - single.mean_cost_usd,
        latency_ms=multi.mean_latency_ms - single.mean_latency_ms,
    )
    if delta.quality > 0:
        conclusion: Literal["multi_improved", "multi_regressed", "mixed_or_equal"] = (
            "multi_improved"
        )
        boundary = "Fixture-only descriptive gain; Live architecture value remains unverified."
    elif delta.quality < 0:
        conclusion = "multi_regressed"
        boundary = "Multi regressed on the frozen Fixture; use single unless Live evidence differs."
    else:
        conclusion = "mixed_or_equal"
        boundary = "No Fixture quality gain; supervisor routing value is operational only."
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
