"""Frozen-holdout Live evaluation contracts for Scenario B attribution."""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

import yaml
from pydantic import Field, model_validator

from oria.agent import (
    ResearchLimits,
    ResearchRunContext,
    build_attribution_graph,
    initial_attribution_state,
)
from oria.core.context import RuntimeServices
from oria.core.types import JsonValue, Principal, ValueModel
from oria.eval.attribution import (
    AttributionBaseline,
    AttributionBlindItem,
    AttributionEvalError,
    AttributionRubric,
    build_attribution_eval_runtime,
    load_attribution_baseline,
    load_attribution_rubric,
)
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import (
    AttributionGoldenCase,
    GoldenDataset,
    load_golden_dataset,
    required_tools_for,
)
from oria.eval.nightly import (
    NightlyBudget,
    NightlyBudgetExceeded,
    NightlyBudgetLedger,
    PricingSnapshot,
    TokenPrices,
)

_ANALYSIS_PERIOD = "2026-08-18/2026-09-01"
_RUNNER_VERSION: Literal["attribution_live_v1"] = "attribution_live_v1"
_OUTCOMES = frozenset({"attributed", "conflicting", "insufficient"})


class AttributionLiveError(RuntimeError):
    """Raised before a Live request or when a bounded run cannot complete safely."""


class AttributionLiveBudget(NightlyBudget):
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
            raise ValueError("Live model-request budget cannot cover the declared case budget")
        if self.max_input_tokens < self.max_cases * self.per_case_max_input_tokens:
            raise ValueError("Live input-token budget cannot cover worst-case reservations")
        if self.max_output_tokens < self.max_cases * self.per_case_max_output_tokens:
            raise ValueError("Live output-token budget cannot cover worst-case reservations")
        return self


class AttributionLiveTarget(ValueModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    provider: Literal["deepseek"]
    model: str = Field(min_length=1)
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    dataset_manifest: str = Field(min_length=1)
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric: str = Field(min_length=1)
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline: str = Field(min_length=1)
    baseline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    split: Literal["holdout"] = "holdout"
    repetitions: Literal[3] = 3
    order_seed: str = Field(min_length=1)
    pricing_snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,127}$")
    rate_tier: Literal["peak", "off_peak"]
    budget: AttributionLiveBudget


class AttributionLiveConfig(ValueModel):
    schema_version: Literal[1] = 1
    targets: tuple[AttributionLiveTarget, ...]

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        if not self.targets or len({item.target_id for item in self.targets}) != len(self.targets):
            raise ValueError("attribution Live targets must be non-empty and unique")
        return self


class AttributionLivePreflight(ValueModel):
    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    target_id: str
    status: Literal["ready", "blocked"]
    request_count: Literal[0] = 0
    checked_at: datetime
    reason: str | None = None
    dataset_version: str | None = None
    dataset_sha256: str | None = None
    holdout_case_count: int | None = Field(default=None, ge=1)
    repetitions: int | None = Field(default=None, ge=1)
    expected_case_runs: int | None = Field(default=None, ge=1)
    pricing_snapshot_id: str | None = None


class AttributionLiveUsage(ValueModel):
    model_requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_basis: Literal["provider_reported", "pricing_upper_bound"]


class AttributionLiveCaseRecord(ValueModel):
    case_id: str
    repetition: int = Field(ge=1)
    expected_outcome: Literal["attributed", "conflicting", "insufficient"]
    observed_outcome: Literal["attributed", "conflicting", "insufficient", "runtime_failure"]
    expected_abstain: bool
    observed_abstain: bool | None
    critical: bool
    automated_pass: bool
    failures: tuple[str, ...]
    failure_taxonomy: tuple[str, ...] = ()
    required_tool_coverage: float = Field(ge=0, le=1)
    forbidden_tool_safe: bool
    evidence_grounded: bool | None
    confidence: float | None = Field(default=None, ge=0, le=1)
    decision_confidence: float | None = Field(default=None, ge=0, le=1)
    conclusion: dict[str, JsonValue] | None
    tool_results: dict[str, dict[str, JsonValue]]
    events: tuple[dict[str, JsonValue], ...]
    provider_request_ids: tuple[str, ...]
    provider_models: tuple[str, ...]
    model_turns: int = Field(ge=0)
    tool_calls_total: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_basis: Literal["provider_reported", "pricing_upper_bound"]
    latency_ms: float = Field(ge=0)
    termination_reason: str | None = None


class AttributionLiveMetrics(ValueModel):
    case_runs: int = Field(ge=1)
    unique_cases: int = Field(ge=1)
    automated_pass_rate: float = Field(ge=0, le=1)
    outcome_accuracy: float = Field(ge=0, le=1)
    abstain_accuracy: float = Field(ge=0, le=1)
    required_tool_coverage: float = Field(ge=0, le=1)
    forbidden_tool_safety_rate: float = Field(ge=0, le=1)
    grounded_evidence_rate: float = Field(ge=0, le=1)
    answer_coverage: float = Field(ge=0, le=1)
    answered_risk: float | None = Field(default=None, ge=0, le=1)


class AttributionRepetitionMetrics(ValueModel):
    repetition: int
    automated_pass_rate: float = Field(ge=0, le=1)
    outcome_accuracy: float = Field(ge=0, le=1)
    abstain_accuracy: float = Field(ge=0, le=1)
    mean_latency_ms: float = Field(ge=0)
    mean_input_tokens: float = Field(ge=0)
    mean_output_tokens: float = Field(ge=0)


class AttributionVariance(ValueModel):
    repetitions: tuple[AttributionRepetitionMetrics, ...]
    automated_pass_rate_stddev: float = Field(ge=0)
    outcome_accuracy_stddev: float = Field(ge=0)
    abstain_accuracy_stddev: float = Field(ge=0)
    latency_ms_stddev: float = Field(ge=0)
    per_case_outcome_consistency_rate: float = Field(ge=0, le=1)


class AttributionConfidenceBin(ValueModel):
    lower: float = Field(ge=0, le=1)
    upper: float = Field(ge=0, le=1)
    count: int = Field(ge=1)
    mean_confidence: float = Field(ge=0, le=1)
    accuracy: float = Field(ge=0, le=1)


class AttributionCalibrationReport(ValueModel):
    status: Literal["descriptive_only"] = "descriptive_only"
    signal: Literal["decision_confidence"] = "decision_confidence"
    sample_size: int = Field(ge=0)
    unique_case_count: int = Field(ge=0)
    bins: tuple[AttributionConfidenceBin, ...]
    brier_score: float | None = Field(default=None, ge=0, le=1)
    expected_calibration_error: float | None = Field(default=None, ge=0, le=1)
    gate_eligible: Literal[False] = False
    reason: str


class AttributionCoverageRiskPoint(ValueModel):
    threshold: float = Field(ge=0, le=1)
    selected_runs: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    risk: float | None = Field(default=None, ge=0, le=1)


class AttributionHumanCalibration(ValueModel):
    status: Literal["pending"] = "pending"
    minimum_sample: int = Field(ge=10)
    selected_blind_items: int = Field(ge=0)
    completed_reviews: Literal[0] = 0
    reviewer: None = None
    judge_status: Literal["not_run"] = "not_run"
    reason: str


class AttributionLiveBlindPacket(ValueModel):
    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    rubric_version: str
    criteria: tuple[dict[str, JsonValue], ...]
    items: tuple[AttributionBlindItem, ...]


class AttributionLiveRunReport(ValueModel):
    schema_version: Literal[1] = 1
    suite: Literal["attribution"] = "attribution"
    runner_version: Literal["attribution_live_v1"] = _RUNNER_VERSION
    verification_level: Literal["live"] = "live"
    status: Literal["completed_pending_human_review", "failed"]
    target_id: str
    provider: str
    model: str
    dataset_version: str
    dataset_sha256: str
    rubric_version: str
    rubric_sha256: str
    baseline_fingerprint: str
    split: Literal["holdout"] = "holdout"
    repetitions: Literal[3] = 3
    expected_case_runs: int
    completed_case_runs: int
    started_at: datetime
    completed_at: datetime
    pricing_snapshot_id: str
    eval_fingerprint: str
    usage: AttributionLiveUsage
    cases: tuple[AttributionLiveCaseRecord, ...]
    metrics: AttributionLiveMetrics
    variance: AttributionVariance
    calibration: AttributionCalibrationReport
    coverage_risk: tuple[AttributionCoverageRiskPoint, ...]
    human_calibration: AttributionHumanCalibration
    reason: str | None = None


def load_attribution_live_config(path: Path) -> AttributionLiveConfig:
    try:
        return AttributionLiveConfig.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AttributionLiveError(
            "attribution Live configuration is unavailable or invalid"
        ) from exc


def _load_pricing(path: Path) -> PricingSnapshot:
    try:
        return PricingSnapshot.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AttributionLiveError("pricing snapshot is unavailable or invalid") from exc


def select_attribution_live_target(
    config: AttributionLiveConfig, target_id: str
) -> AttributionLiveTarget:
    target = next((item for item in config.targets if item.target_id == target_id), None)
    if target is None:
        raise AttributionLiveError("attribution Live target is not configured")
    return target


def _asset_path(config_path: Path, relative: str) -> Path:
    return (config_path.parents[1] / relative).resolve()


def _validated_assets(
    *, config_path: Path, pricing_dir: Path, target: AttributionLiveTarget, now: datetime
) -> tuple[GoldenDataset, AttributionRubric, AttributionBaseline, PricingSnapshot]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise AttributionLiveError("attribution Live preflight time must include a timezone")
    dataset = load_golden_dataset(_asset_path(config_path, target.dataset_manifest))
    manifest = dataset.manifest
    if (
        manifest.suite != "scenario_b"
        or manifest.dataset_version != target.dataset_version
        or manifest.dataset_sha256 != target.dataset_sha256
        or manifest.review_status != "approved"
        or not manifest.human_review_complete
        or not manifest.baseline_created
        or manifest.holdout_frozen is not True
    ):
        raise AttributionLiveError("frozen reviewed attribution dataset identity is invalid")
    holdout = tuple(
        case
        for case in dataset.cases
        if isinstance(case, AttributionGoldenCase) and case.split == "holdout"
    )
    if len(holdout) != manifest.holdout_case_count:
        raise AttributionLiveError("frozen holdout count does not match its manifest")
    if {case.expected_outcome for case in holdout} != _OUTCOMES:
        raise AttributionLiveError("frozen holdout does not cover all three attribution scenarios")
    if len(holdout) * target.repetitions != target.budget.max_cases:
        raise AttributionLiveError("frozen holdout repetitions do not match max_cases")

    rubric_path = _asset_path(config_path, target.rubric)
    rubric = load_attribution_rubric(rubric_path)
    rubric_sha256 = hashlib.sha256(rubric_path.read_bytes()).hexdigest()
    if (
        rubric.dataset_version != target.dataset_version
        or rubric_sha256 != target.rubric_sha256
        or manifest.rubric_sha256 != target.rubric_sha256
    ):
        raise AttributionLiveError("attribution rubric identity is invalid")
    baseline = load_attribution_baseline(_asset_path(config_path, target.baseline))
    if (
        baseline.dataset_version != target.dataset_version
        or baseline.dataset_sha256 != target.dataset_sha256
        or baseline.rubric_sha256 != target.rubric_sha256
        or baseline.eval_fingerprint != target.baseline_fingerprint
    ):
        raise AttributionLiveError("attribution Fixture baseline identity is invalid")

    snapshot = _load_pricing(pricing_dir / f"{target.pricing_snapshot_id}.yaml")
    if snapshot.snapshot_id != target.pricing_snapshot_id:
        raise AttributionLiveError("pricing snapshot identity does not match its filename")
    if now > snapshot.valid_until:
        raise AttributionLiveError("pricing snapshot is expired")
    if target.model not in snapshot.models:
        raise AttributionLiveError("pricing snapshot does not cover the selected model")
    return dataset, rubric, baseline, snapshot


def preflight_attribution_live(
    *,
    config_path: Path,
    pricing_dir: Path,
    target_id: str,
    environ: Mapping[str, str],
    now: datetime,
    known_targets: frozenset[str],
) -> AttributionLivePreflight:
    """Validate every frozen identity and credential without creating a provider."""

    try:
        config = load_attribution_live_config(config_path)
        if target_id not in known_targets:
            raise AttributionLiveError("attribution Live target is unknown")
        target = select_attribution_live_target(config, target_id)
        dataset, _, _, snapshot = _validated_assets(
            config_path=config_path,
            pricing_dir=pricing_dir,
            target=target,
            now=now,
        )
        if not environ.get(target.credential_env, "").strip():
            holdout_count = cast(int, dataset.manifest.holdout_case_count)
            return AttributionLivePreflight(
                target_id=target_id,
                status="blocked",
                checked_at=now,
                reason="attribution Live credential is missing",
                dataset_version=dataset.manifest.dataset_version,
                dataset_sha256=dataset.manifest.dataset_sha256,
                holdout_case_count=holdout_count,
                repetitions=target.repetitions,
                expected_case_runs=holdout_count * target.repetitions,
                pricing_snapshot_id=snapshot.snapshot_id,
            )
    except (AttributionLiveError, AttributionEvalError, ValueError) as exc:
        return AttributionLivePreflight(
            target_id=target_id,
            status="blocked",
            checked_at=now,
            reason=str(exc),
        )
    holdout_count = cast(int, dataset.manifest.holdout_case_count)
    return AttributionLivePreflight(
        target_id=target_id,
        status="ready",
        checked_at=now,
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        holdout_case_count=holdout_count,
        repetitions=target.repetitions,
        expected_case_runs=holdout_count * target.repetitions,
        pricing_snapshot_id=snapshot.snapshot_id,
    )


def _principal(tenant_id: str, *, kind: Literal["human", "service"]) -> Principal:
    return Principal(
        subject_id="live-reviewer" if kind == "human" else "live-runner",
        tenant_id=tenant_id,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="trusted-live-eval",
    )


def _case_order(
    cases: tuple[AttributionGoldenCase, ...], *, seed: str, repetition: int
) -> tuple[AttributionGoldenCase, ...]:
    buckets: dict[str, list[AttributionGoldenCase]] = {outcome: [] for outcome in _OUTCOMES}
    for case in cases:
        buckets[case.expected_outcome].append(case)
    rng = random.Random(f"{seed}:{repetition}")
    for bucket in buckets.values():
        rng.shuffle(bucket)
    ordered: list[AttributionGoldenCase] = []
    while any(buckets.values()):
        for outcome in sorted(_OUTCOMES):
            if buckets[outcome]:
                ordered.append(buckets[outcome].pop())
    return tuple(ordered)


def _event_strings(events: tuple[dict[str, JsonValue], ...], key: str) -> tuple[str, ...]:
    values: list[str] = []
    for event in events:
        value = event.get(key)
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(values)


def _pricing_cost(input_tokens: int, output_tokens: int, prices: TokenPrices) -> float:
    return (
        input_tokens * prices.input_cache_miss_per_million_usd
        + output_tokens * max(prices.output_per_million_usd, prices.reasoning_per_million_usd)
    ) / 1_000_000


_FAILURE_TAXONOMY = {
    "required_tool_missing": "B_evidence_retrieval",
    "forbidden_tool_executed": "F_tool_policy",
    "outcome_mismatch": "D_outcome_mapping",
    "abstain_mismatch": "D_outcome_mapping",
    "evidence_not_grounded": "C_evidence_interpretation",
    "runtime_termination": "B_evidence_retrieval",
    "provider_request_id_missing": "H_evaluator_infrastructure",
    "hypothesis_mismatch": "E_hypothesis_rendering",
}


def _failure_taxonomy(failures: tuple[str, ...], termination_reason: str | None) -> tuple[str, ...]:
    """Map raw failures to a coarse capability taxonomy for multi-dim reporting."""
    codes: list[str] = []
    for failure in failures:
        taxonomy = _FAILURE_TAXONOMY.get(failure)
        if taxonomy is not None and taxonomy not in codes:
            codes.append(taxonomy)
    if termination_reason in {"structured_output_error", "schema_validation_failed"}:
        code = "E_hypothesis_rendering"
        if code not in codes:
            codes.append(code)
    elif termination_reason in {"policy_or_contract_violation"}:
        code = "F_tool_policy"
        if code not in codes:
            codes.append(code)
    elif termination_reason in {"max_tool_calls", "max_model_turns", "deadline_exceeded"}:
        code = "B_evidence_retrieval"
        if code not in codes:
            codes.append(code)
    return tuple(sorted(codes))


def _case_record(
    *,
    case: AttributionGoldenCase,
    repetition: int,
    state: dict[str, Any],
    prices: TokenPrices,
    latency_ms: float,
) -> AttributionLiveCaseRecord:
    conclusion = cast(dict[str, JsonValue] | None, state.get("conclusion"))
    termination = cast(dict[str, JsonValue] | None, state.get("termination"))
    observed_outcome = cast(
        Literal["attributed", "conflicting", "insufficient", "runtime_failure"],
        "runtime_failure" if conclusion is None else conclusion["outcome"],
    )
    observed_abstain = None if conclusion is None else cast(bool, conclusion["abstained"])
    confidence = None if conclusion is None else cast(float, conclusion["confidence"])
    decision_confidence = (
        None
        if confidence is None or observed_abstain is None
        else (1.0 - confidence if observed_abstain else confidence)
    )
    tool_results = cast(dict[str, dict[str, JsonValue]], state.get("tool_results", {}))
    executed_tools = tuple(cast(str, value["tool_name"]) for value in tool_results.values())
    required = set(required_tools_for(case))
    coverage = 1.0 if not required else len(required.intersection(executed_tools)) / len(required)
    forbidden_safe = not bool(set(case.forbidden_tools).intersection(executed_tools))
    evidence_grounded = (
        None if conclusion is None or observed_outcome == "insufficient" else termination is None
    )
    events = tuple(cast(list[dict[str, JsonValue]], state.get("events", [])))
    provider_request_ids = _event_strings(events, "provider_request_id")
    model_turns = cast(int, state.get("model_turns", 0))
    failures: list[str] = []
    if observed_outcome != case.expected_outcome:
        failures.append("outcome_mismatch")
    if observed_abstain != case.expected_abstain:
        failures.append("abstain_mismatch")
    if coverage < 1:
        failures.append("required_tool_missing")
    if not forbidden_safe:
        failures.append("forbidden_tool_executed")
    if case.expected_outcome != "insufficient" and evidence_grounded is not True:
        failures.append("evidence_not_grounded")
    if termination is not None:
        failures.append("runtime_termination")
    if len(provider_request_ids) != model_turns:
        failures.append("provider_request_id_missing")
    input_tokens = cast(int, state.get("input_tokens", 0))
    output_tokens = cast(int, state.get("output_tokens", 0))
    provider_cost = cast(float, state.get("total_cost", 0.0))
    estimated_cost = _pricing_cost(input_tokens, output_tokens, prices)
    termination_reason = None if termination is None else cast(str, termination.get("reason"))
    return AttributionLiveCaseRecord(
        case_id=case.case_id,
        repetition=repetition,
        expected_outcome=case.expected_outcome,
        observed_outcome=observed_outcome,
        expected_abstain=case.expected_abstain,
        observed_abstain=observed_abstain,
        critical=case.critical,
        automated_pass=not failures,
        failures=tuple(failures),
        failure_taxonomy=_failure_taxonomy(tuple(failures), termination_reason),
        required_tool_coverage=coverage,
        forbidden_tool_safe=forbidden_safe,
        evidence_grounded=evidence_grounded,
        confidence=confidence,
        decision_confidence=decision_confidence,
        conclusion=conclusion,
        tool_results=tool_results,
        events=events,
        provider_request_ids=provider_request_ids,
        provider_models=_event_strings(events, "provider_model"),
        model_turns=model_turns,
        tool_calls_total=cast(int, state.get("tool_calls_total", 0)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=provider_cost if provider_cost > 0 else estimated_cost,
        cost_basis="provider_reported" if provider_cost > 0 else "pricing_upper_bound",
        latency_ms=latency_ms,
        termination_reason=termination_reason,
    )


def attribution_live_metrics(
    records: tuple[AttributionLiveCaseRecord, ...],
) -> AttributionLiveMetrics:
    if not records:
        raise AttributionLiveError("attribution Live metrics require case records")
    answered = tuple(record for record in records if record.observed_abstain is False)
    grounded = tuple(record for record in records if record.expected_outcome != "insufficient")
    return AttributionLiveMetrics(
        case_runs=len(records),
        unique_cases=len({record.case_id for record in records}),
        automated_pass_rate=sum(record.automated_pass for record in records) / len(records),
        outcome_accuracy=sum(
            record.observed_outcome == record.expected_outcome for record in records
        )
        / len(records),
        abstain_accuracy=sum(
            record.observed_abstain == record.expected_abstain for record in records
        )
        / len(records),
        required_tool_coverage=sum(record.required_tool_coverage for record in records)
        / len(records),
        forbidden_tool_safety_rate=sum(record.forbidden_tool_safe for record in records)
        / len(records),
        grounded_evidence_rate=(
            sum(record.evidence_grounded is True for record in grounded) / len(grounded)
            if grounded
            else 1.0
        ),
        answer_coverage=len(answered) / len(records),
        answered_risk=(
            1 - sum(record.automated_pass for record in answered) / len(answered)
            if answered
            else None
        ),
    )


def _stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def attribution_live_variance(
    records: tuple[AttributionLiveCaseRecord, ...],
) -> AttributionVariance:
    repetitions: list[AttributionRepetitionMetrics] = []
    for repetition in sorted({record.repetition for record in records}):
        selected = tuple(record for record in records if record.repetition == repetition)
        count = len(selected)
        repetitions.append(
            AttributionRepetitionMetrics(
                repetition=repetition,
                automated_pass_rate=sum(record.automated_pass for record in selected) / count,
                outcome_accuracy=sum(
                    record.observed_outcome == record.expected_outcome for record in selected
                )
                / count,
                abstain_accuracy=sum(
                    record.observed_abstain == record.expected_abstain for record in selected
                )
                / count,
                mean_latency_ms=sum(record.latency_ms for record in selected) / count,
                mean_input_tokens=sum(record.input_tokens for record in selected) / count,
                mean_output_tokens=sum(record.output_tokens for record in selected) / count,
            )
        )
    case_ids = {record.case_id for record in records}
    consistent = sum(
        len({record.observed_outcome for record in records if record.case_id == case_id}) == 1
        for case_id in case_ids
    )
    return AttributionVariance(
        repetitions=tuple(repetitions),
        automated_pass_rate_stddev=_stddev([item.automated_pass_rate for item in repetitions]),
        outcome_accuracy_stddev=_stddev([item.outcome_accuracy for item in repetitions]),
        abstain_accuracy_stddev=_stddev([item.abstain_accuracy for item in repetitions]),
        latency_ms_stddev=_stddev([item.mean_latency_ms for item in repetitions]),
        per_case_outcome_consistency_rate=consistent / len(case_ids),
    )


def attribution_calibration(
    records: tuple[AttributionLiveCaseRecord, ...],
) -> AttributionCalibrationReport:
    values = tuple(record for record in records if record.decision_confidence is not None)
    boundaries = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))
    bins: list[AttributionConfidenceBin] = []
    for index, (lower, upper) in enumerate(boundaries):
        selected = tuple(
            record
            for record in values
            if cast(float, record.decision_confidence) >= lower
            and (
                cast(float, record.decision_confidence) < upper
                or (index == len(boundaries) - 1 and record.decision_confidence == 1.0)
            )
        )
        if not selected:
            continue
        bins.append(
            AttributionConfidenceBin(
                lower=lower,
                upper=upper,
                count=len(selected),
                mean_confidence=sum(cast(float, item.decision_confidence) for item in selected)
                / len(selected),
                accuracy=sum(item.automated_pass for item in selected) / len(selected),
            )
        )
    brier = (
        sum(
            (cast(float, record.decision_confidence) - float(record.automated_pass)) ** 2
            for record in values
        )
        / len(values)
        if values
        else None
    )
    ece = (
        sum(item.count / len(values) * abs(item.accuracy - item.mean_confidence) for item in bins)
        if values
        else None
    )
    return AttributionCalibrationReport(
        sample_size=len(values),
        unique_case_count=len({record.case_id for record in values}),
        bins=tuple(bins),
        brier_score=brier,
        expected_calibration_error=ece,
        reason=(
            "Brier/ECE are descriptive because repeated runs are correlated and semantic "
            "correctness still requires calibrated human review."
        ),
    )


def attribution_coverage_risk(
    records: tuple[AttributionLiveCaseRecord, ...],
) -> tuple[AttributionCoverageRiskPoint, ...]:
    values = tuple(record for record in records if record.decision_confidence is not None)
    points: list[AttributionCoverageRiskPoint] = []
    for threshold in (0.0, 0.5, 0.7, 0.8, 0.9):
        selected = tuple(
            record for record in values if cast(float, record.decision_confidence) >= threshold
        )
        points.append(
            AttributionCoverageRiskPoint(
                threshold=threshold,
                selected_runs=len(selected),
                coverage=len(selected) / len(values) if values else 0.0,
                risk=(
                    1 - sum(record.automated_pass for record in selected) / len(selected)
                    if selected
                    else None
                ),
            )
        )
    return tuple(points)


def _blind_packet(
    *,
    records: tuple[AttributionLiveCaseRecord, ...],
    cases_by_id: dict[str, AttributionGoldenCase],
    dataset_sha256: str,
    rubric: AttributionRubric,
) -> AttributionLiveBlindPacket:
    selected: list[AttributionLiveCaseRecord] = []
    for outcome in sorted(_OUTCOMES):
        candidates = [record for record in records if record.expected_outcome == outcome]
        selected.extend(candidates[:3])
    remainder = [record for record in records if record not in selected]
    selected.extend(remainder[: max(0, rubric.calibration.minimum_human_sample - len(selected))])
    items: list[AttributionBlindItem] = []
    for record in selected:
        case = cases_by_id[record.case_id]
        conclusion = record.conclusion or {}
        blind_id = hashlib.sha256(
            f"{dataset_sha256}:{record.case_id}:{record.repetition}".encode()
        ).hexdigest()[:16]
        items.append(
            AttributionBlindItem(
                blind_case_id=f"blind_{blind_id}",
                conversation_history=case.conversation_history,
                question=case.question,
                observed_outcome=record.observed_outcome,
                conclusion=cast(str | None, conclusion.get("conclusion")),
                hypotheses=tuple(
                    cast(list[dict[str, JsonValue]], conclusion.get("hypotheses", []))
                ),
                evidence=tuple(cast(list[dict[str, JsonValue]], conclusion.get("evidence", []))),
                requested_data=tuple(cast(list[str], conclusion.get("requested_data", []))),
            )
        )
    criteria = tuple(
        cast(dict[str, JsonValue], criterion.model_dump(mode="json"))
        for criterion in rubric.criteria
    )
    return AttributionLiveBlindPacket(
        rubric_version=rubric.rubric_version,
        criteria=criteria,
        items=tuple(items),
    )


async def run_attribution_live(
    *,
    config_path: Path,
    pricing_dir: Path,
    target: AttributionLiveTarget,
    base_runtime: RuntimeServices,
    data_dir: Path,
    started_at: datetime,
) -> tuple[AttributionLiveRunReport, AttributionLiveBlindPacket]:
    """Run the complete frozen holdout three times through the selected real provider."""

    dataset, rubric, baseline, snapshot = _validated_assets(
        config_path=config_path,
        pricing_dir=pricing_dir,
        target=target,
        now=started_at,
    )
    if base_runtime.llm is None:
        raise AttributionLiveError("selected Live provider is unavailable")
    if (
        base_runtime.config.llm.provider != target.provider
        or base_runtime.config.llm.model != target.model
    ):
        raise AttributionLiveError("runtime provider/model does not match the frozen Live target")
    prices = getattr(snapshot.models[target.model], target.rate_tier)
    cases = tuple(
        case
        for case in dataset.cases
        if isinstance(case, AttributionGoldenCase) and case.split == "holdout"
    )
    query_databases: dict[str, Path] = {}
    for variant in sorted({case.fixture_variant for case in cases}):
        query_database = data_dir / "fixtures" / variant / "analytics.db"
        label_database = data_dir / "evaluation-only" / variant / "labels.db"
        generate_attribution_fixture(query_database, label_database, fixture_variant=variant)
        query_databases[variant] = query_database

    graph = build_attribution_graph()
    ledger = NightlyBudgetLedger(target.budget, prices)
    records: list[AttributionLiveCaseRecord] = []
    reason: str | None = None
    for repetition in range(1, target.repetitions + 1):
        for case in _case_order(cases, seed=target.order_seed, repetition=repetition):
            try:
                reservation = ledger.reserve(
                    input_tokens=target.budget.per_case_max_input_tokens,
                    max_output_tokens=target.budget.per_case_max_output_tokens,
                )
            except NightlyBudgetExceeded as exc:
                reason = str(exc)
                break
            actor = _principal(case.tenant_id, kind="human")
            executor = _principal(case.tenant_id, kind="service")
            runtime = build_attribution_eval_runtime(
                base_runtime,
                (case,),
                query_databases[case.fixture_variant],
                llm=base_runtime.llm,
                trusted_actors=(actor,),
                trusted_executors=(executor,),
            )
            run_id = f"{case.case_id}-r{repetition}"
            ctx = runtime.new_context(
                actor=actor,
                executor=executor,
                session_id="attribution-live-v1",
                thread_id=run_id,
                run_id=run_id,
            )
            started = time.perf_counter()
            try:
                state = await graph.ainvoke(
                    initial_attribution_state(
                        question=case.question,
                        analysis_period=_ANALYSIS_PERIOD,
                        conversation_history=case.conversation_history,
                        tenant_id=case.tenant_id,
                    ),
                    context=ResearchRunContext(
                        ctx=ctx,
                        limits=ResearchLimits(
                            max_model_turns=target.budget.per_case_max_model_turns,
                            max_tool_calls=target.budget.per_case_max_tool_calls,
                            max_input_tokens=target.budget.per_case_max_input_tokens,
                            max_output_tokens=target.budget.per_case_max_output_tokens,
                            max_total_tokens=(
                                target.budget.per_case_max_input_tokens
                                + target.budget.per_case_max_output_tokens
                            ),
                            max_cost=target.budget.per_case_max_cost_usd,
                            max_validation_repairs=2,
                        ),
                        deadline_at=datetime.now(UTC)
                        + timedelta(seconds=target.budget.per_case_timeout_seconds),
                    ),
                )
            except Exception as exc:
                ledger.cancel(reservation)
                reason = f"Live case execution failed: {type(exc).__name__}"
                await runtime.aclose()
                break
            latency_ms = (time.perf_counter() - started) * 1000
            record = _case_record(
                case=case,
                repetition=repetition,
                state=cast(dict[str, Any], state),
                prices=prices,
                latency_ms=latency_ms,
            )
            try:
                ledger.settle(
                    reservation,
                    cache_hit_tokens=0,
                    cache_miss_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    reasoning_tokens=0,
                )
            except NightlyBudgetExceeded as exc:
                reason = str(exc)
                await runtime.aclose()
                break
            records.append(record)
            await runtime.aclose()
            if record.provider_models and set(record.provider_models) != {target.model}:
                reason = "provider response model does not match the frozen Live target"
                break
            if sum(item.model_turns for item in records) > target.budget.max_model_requests:
                reason = "attribution Live model-request budget is exhausted"
                break
        if reason is not None:
            break

    completed_at = datetime.now().astimezone()
    record_tuple = tuple(records)
    if not record_tuple:
        raise AttributionLiveError(reason or "attribution Live run produced no case records")
    metrics = attribution_live_metrics(record_tuple)
    variance = attribution_live_variance(record_tuple)
    calibration = attribution_calibration(record_tuple)
    coverage_risk = attribution_coverage_risk(record_tuple)
    blind_packet = _blind_packet(
        records=record_tuple,
        cases_by_id={case.case_id: case for case in cases},
        dataset_sha256=dataset.manifest.dataset_sha256,
        rubric=rubric,
    )
    expected = len(cases) * target.repetitions
    complete = (
        reason is None and len(records) == expected and ledger.complete(expected_cases=expected)
    )
    fingerprint_payload = {
        "runner_version": _RUNNER_VERSION,
        "target_id": target.target_id,
        "provider": target.provider,
        "model": target.model,
        "dataset_version": target.dataset_version,
        "dataset_sha256": target.dataset_sha256,
        "rubric_sha256": target.rubric_sha256,
        "baseline_fingerprint": target.baseline_fingerprint,
        "repetitions": target.repetitions,
        "order_seed": target.order_seed,
        "budget": target.budget.model_dump(mode="json"),
        "pricing_snapshot_id": target.pricing_snapshot_id,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    provider_reported = all(item.cost_basis == "provider_reported" for item in records)
    report = AttributionLiveRunReport(
        status="completed_pending_human_review" if complete else "failed",
        target_id=target.target_id,
        provider=target.provider,
        model=target.model,
        dataset_version=target.dataset_version,
        dataset_sha256=target.dataset_sha256,
        rubric_version=rubric.rubric_version,
        rubric_sha256=target.rubric_sha256,
        baseline_fingerprint=baseline.eval_fingerprint,
        repetitions=target.repetitions,
        expected_case_runs=expected,
        completed_case_runs=len(records),
        started_at=started_at,
        completed_at=completed_at,
        pricing_snapshot_id=target.pricing_snapshot_id,
        eval_fingerprint=f"sha256:{fingerprint}",
        usage=AttributionLiveUsage(
            model_requests=sum(item.model_turns for item in records),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            cost_usd=sum(item.cost_usd for item in records),
            cost_basis="provider_reported" if provider_reported else "pricing_upper_bound",
        ),
        cases=record_tuple,
        metrics=metrics,
        variance=variance,
        calibration=calibration,
        coverage_risk=coverage_risk,
        human_calibration=AttributionHumanCalibration(
            minimum_sample=rubric.calibration.minimum_human_sample,
            selected_blind_items=len(blind_packet.items),
            reason=(
                "A human must independently score at least ten architecture-blind items before "
                "the Live card can be accepted; no LLM judge was used as a substitute."
            ),
        ),
        reason=reason,
    )
    return report, blind_packet
