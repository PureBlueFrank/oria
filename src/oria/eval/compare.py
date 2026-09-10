"""Fair single-agent versus multi-agent comparison contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from oria.core.types import ValueModel
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
    execution_order: tuple[Architecture, ...]
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
