"""Deterministic Scenario B attribution evaluation and blind-review contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, model_validator

from oria.agent import (
    ResearchRunContext,
    attribution_research_limits,
    build_attribution_graph,
    initial_attribution_state,
)
from oria.analytics.query import AnalyticsQueryStore
from oria.config import resolve_runtime_config
from oria.core.context import Context, RuntimeServices, SealedAsyncExitStack
from oria.core.protocols import LLMProvider
from oria.core.runtime import build_runtime
from oria.core.types import (
    ChatOptions,
    ChatResult,
    Done,
    JsonValue,
    Message,
    Principal,
    ProviderCapabilities,
    StreamEvent,
    TextBlock,
    ToolCall,
    ToolSpec,
    Usage,
    ValueModel,
)
from oria.data import initialize_data
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import AttributionGoldenCase, GoldenDataset, load_golden_dataset
from oria.permission.local import LocalPolicyEngine
from oria.tools.analytics import build_attribution_tool_registry

AttributionSplit = Literal["development", "holdout", "all"]
_RUNNER_VERSION: Literal["attribution_fixture_v1"] = "attribution_fixture_v1"
_ANALYSIS_PERIOD = "2026-08-18/2026-09-01"
_QUERY_PERIOD = cast(
    dict[str, JsonValue],
    {"start_date": "2026-08-18", "end_date": "2026-09-01"},
)
_MARKET_PERIOD = cast(
    dict[str, JsonValue],
    {"start_date": "2026-08-31", "end_date": "2026-09-01"},
)


class AttributionEvalError(RuntimeError):
    """Raised when an attribution evaluation asset or run violates its contract."""


class AttributionScoreScale(ValueModel):
    minimum: Literal[0]
    maximum: Literal[2]


class AttributionRubricCriterion(ValueModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)
    anchors: dict[Literal["0", "1", "2"], str]

    @model_validator(mode="after")
    def require_complete_anchors(self) -> AttributionRubricCriterion:
        if set(self.anchors) != {"0", "1", "2"} or any(
            not value.strip() for value in self.anchors.values()
        ):
            raise ValueError("attribution rubric criteria require non-empty 0/1/2 anchors")
        return self


class AttributionDecisionRules(ValueModel):
    pass_score: float = Field(ge=0, le=1)
    critical_minimum_per_criterion: Literal[2]
    require_all_evidence_refs_grounded: Literal[True]
    require_forbidden_tool_safety: Literal[True]


class AttributionCalibration(ValueModel):
    required: Literal[True]
    minimum_human_sample: int = Field(ge=10)
    instructions: str = Field(min_length=1)


class AttributionRubric(ValueModel):
    suite: Literal["attribution"]
    rubric_version: Literal["1"]
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    score_scale: AttributionScoreScale
    blind_input_fields: tuple[str, ...]
    hidden_fields: tuple[str, ...]
    criteria: tuple[AttributionRubricCriterion, ...] = Field(min_length=1)
    decision_rules: AttributionDecisionRules
    calibration: AttributionCalibration

    @model_validator(mode="after")
    def validate_blind_contract(self) -> AttributionRubric:
        criterion_ids = tuple(item.id for item in self.criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("attribution rubric criterion IDs must be unique")
        if abs(sum(item.weight for item in self.criteria) - 1.0) > 1e-9:
            raise ValueError("attribution rubric criterion weights must sum to one")
        forbidden = {
            "split",
            "critical",
            "root_cause_code",
            "acceptable_hypotheses",
            "required_evidence",
            "golden_rationale",
            "expected_tools",
            "forbidden_tools",
            "provider_profile",
            "architecture_label",
        }
        if forbidden.difference(self.hidden_fields):
            raise ValueError("attribution rubric must hide labels and architecture identity")
        if forbidden.intersection(self.blind_input_fields):
            raise ValueError("attribution blind inputs expose hidden labels")
        return self


class AttributionCaseResult(ValueModel):
    case_id: str
    split: Literal["development", "holdout"]
    critical: bool
    passed: bool
    observed_outcome: Literal["attributed", "conflicting", "insufficient", "runtime_failure"]
    termination_reason: str | None = None
    abstained: bool | None
    executed_tools: tuple[str, ...]
    evidence_grounded: bool | None
    hypothesis_match: bool
    confidence: float | None = Field(default=None, ge=0, le=1)
    failures: tuple[str, ...] = ()


class AttributionMetrics(ValueModel):
    evaluated_cases: int = Field(ge=1)
    case_pass_rate: float = Field(ge=0, le=1)
    critical_pass_rate: float = Field(ge=0, le=1)
    outcome_accuracy: float = Field(ge=0, le=1)
    abstain_accuracy: float = Field(ge=0, le=1)
    required_tool_coverage: float = Field(ge=0, le=1)
    forbidden_tool_safety_rate: float = Field(ge=0, le=1)
    grounded_evidence_rate: float = Field(ge=0, le=1)


class AttributionBlindItem(ValueModel):
    blind_case_id: str = Field(pattern=r"^blind_[0-9a-f]{16}$")
    conversation_history: tuple[Message, ...] = ()
    question: str
    observed_outcome: Literal["attributed", "conflicting", "insufficient", "runtime_failure"]
    conclusion: str | None
    hypotheses: tuple[dict[str, JsonValue], ...]
    evidence: tuple[dict[str, JsonValue], ...]
    requested_data: tuple[str, ...]


class AttributionEvalReport(ValueModel):
    suite: Literal["attribution"] = "attribution"
    runner_version: Literal["attribution_fixture_v1"] = _RUNNER_VERSION
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_version: str
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: AttributionSplit
    verification_level: Literal["fixture"] = "fixture"
    provider_profile: Literal["attribution_replay_v1"] = "attribution_replay_v1"
    tool_schema_versions: dict[str, int]
    eval_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    executed_at: datetime
    cases: tuple[AttributionCaseResult, ...]
    metrics: AttributionMetrics
    blind_review_items: tuple[AttributionBlindItem, ...]


class AttributionBaseline(ValueModel):
    suite: Literal["attribution"] = "attribution"
    runner_version: Literal["attribution_fixture_v1"] = _RUNNER_VERSION
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_version: str
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: Literal["all"] = "all"
    verification_level: Literal["fixture"] = "fixture"
    provider_profile: Literal["attribution_replay_v1"] = "attribution_replay_v1"
    tool_schema_versions: dict[str, int]
    eval_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    cases: tuple[AttributionCaseResult, ...]
    metrics: AttributionMetrics


class AttributionGates(ValueModel):
    suite: Literal["attribution"]
    dataset_version: str
    allowed_regression: Literal[0] = 0
    required_metrics: dict[str, float]

    @model_validator(mode="after")
    def validate_metrics(self) -> AttributionGates:
        expected = {
            "case_pass_rate",
            "critical_pass_rate",
            "outcome_accuracy",
            "abstain_accuracy",
            "required_tool_coverage",
            "forbidden_tool_safety_rate",
            "grounded_evidence_rate",
        }
        if set(self.required_metrics) != expected:
            raise ValueError("attribution gates must configure every deterministic metric")
        if any(value < 0 or value > 1 for value in self.required_metrics.values()):
            raise ValueError("attribution gate metrics must be between zero and one")
        return self


def load_attribution_rubric(path: Path) -> AttributionRubric:
    try:
        return AttributionRubric.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AttributionEvalError("attribution rubric is unavailable or invalid") from exc


def load_attribution_gates(path: Path) -> AttributionGates:
    try:
        return AttributionGates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AttributionEvalError("attribution gates are unavailable or invalid") from exc


def load_attribution_baseline(path: Path) -> AttributionBaseline:
    try:
        return AttributionBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AttributionEvalError("attribution baseline is unavailable or invalid") from exc


def create_attribution_baseline(
    report: AttributionEvalReport,
    *,
    created_at: datetime,
) -> AttributionBaseline:
    if report.split != "all":
        raise AttributionEvalError("attribution baseline requires the complete dataset")
    if not all(case.passed for case in report.cases):
        raise AttributionEvalError("cannot create an attribution baseline from failing cases")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise AttributionEvalError("attribution baseline time must include a timezone")
    return AttributionBaseline(
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        rubric_version=report.rubric_version,
        rubric_sha256=report.rubric_sha256,
        tool_schema_versions=report.tool_schema_versions,
        eval_fingerprint=report.eval_fingerprint,
        created_at=created_at,
        cases=report.cases,
        metrics=report.metrics,
    )


def assert_attribution_gates(
    report: AttributionEvalReport,
    *,
    gates: AttributionGates,
    baseline: AttributionBaseline | None = None,
) -> None:
    if report.suite != gates.suite or report.dataset_version != gates.dataset_version:
        raise AttributionEvalError("attribution report and gates identify different suites")
    metrics = report.metrics.model_dump()
    for name, required in gates.required_metrics.items():
        if metrics[name] < required:
            raise AttributionEvalError(f"attribution required metric failed: {name}")
    failed_critical = [case.case_id for case in report.cases if case.critical and not case.passed]
    if failed_critical:
        raise AttributionEvalError(
            f"attribution critical cases failed: {','.join(failed_critical)}"
        )
    if baseline is None:
        return
    identity = (
        report.dataset_version,
        report.dataset_sha256,
        report.rubric_version,
        report.rubric_sha256,
        report.runner_version,
        report.provider_profile,
        report.tool_schema_versions,
        report.eval_fingerprint,
    )
    baseline_identity = (
        baseline.dataset_version,
        baseline.dataset_sha256,
        baseline.rubric_version,
        baseline.rubric_sha256,
        baseline.runner_version,
        baseline.provider_profile,
        baseline.tool_schema_versions,
        baseline.eval_fingerprint,
    )
    if identity != baseline_identity:
        raise AttributionEvalError("attribution report does not match the frozen baseline identity")
    if report.split != "all" or report.cases != baseline.cases:
        raise AttributionEvalError("attribution per-case behavior regressed from baseline")
    baseline_metrics = baseline.metrics.model_dump()
    for name, prior in baseline_metrics.items():
        if metrics[name] < prior - gates.allowed_regression:
            raise AttributionEvalError(f"attribution metric regressed from baseline: {name}")


def _case_scope(case: AttributionGoldenCase) -> tuple[str | None, str | None]:
    question = case.question
    regions = {
        name
        for label, name in (
            ("华东", "east"),
            ("北区", "north"),
            ("华北", "north"),
            ("华南", "south"),
        )
        if label in question
    }
    categories = {
        name
        for label, name in (
            ("正餐", "full_service"),
            ("快餐", "quick_service"),
            ("饮料", "beverage"),
            ("零售", "retail"),
        )
        if label in question
    }
    needs_region_control = case.fixture_variant in {
        "campaign_effect",
        "market_conflict",
        "systemic_category",
        "campaign_enrollment",
        "campaign_confirmation",
    } or any(term in question for term in ("其他区域", "两地", "各区域"))
    region = None if needs_region_control or len(regions) != 1 else next(iter(regions))
    category = None if len(categories) != 1 else next(iter(categories))
    return region, category


def _tool_args(tool_name: str, case: AttributionGoldenCase) -> dict[str, JsonValue]:
    region, category = _case_scope(case)
    if tool_name == "query_funnel":
        dimensions = ["event_date"]
        if region is None:
            dimensions.append("region")
        if category is None:
            dimensions.append("category")
        return cast(
            dict[str, JsonValue],
            {
                "period": _QUERY_PERIOD,
                "dimensions": dimensions[:3],
                "region": region,
                "category": category,
            },
        )
    if tool_name == "drill_down":
        if category is not None and region is None:
            dimension, value, group_by = "category", category, ["event_date", "region"]
        else:
            dimension, value, group_by = "region", region or "east", ["event_date", "category"]
        return cast(
            dict[str, JsonValue],
            {
                "period": _QUERY_PERIOD,
                "dimension": dimension,
                "value": value,
                "group_by": group_by,
            },
        )
    if tool_name == "query_activity":
        merchant_id = {
            "sb-v1-011": "synthetic-merchant-unlisted",
            "sb-v1-029": "synthetic-merchant-secondary",
        }.get(case.case_id)
        return cast(
            dict[str, JsonValue],
            {
                "period": _QUERY_PERIOD,
                "category": None if merchant_id is not None else category or "full_service",
                "merchant_id": merchant_id,
            },
        )
    if tool_name == "query_market_overview":
        return cast(
            dict[str, JsonValue],
            {
                "period": _MARKET_PERIOD,
                "comparison": "previous_period",
                "dimensions": ["region", "category"],
                "region": region,
                "category": category,
            },
        )
    if tool_name == "search_history_experience":
        return {"query": case.question, "limit": 3}
    raise AttributionEvalError("attribution Golden references an unknown expected tool")


def _message_value(message: Message | object) -> dict[str, Any]:
    if isinstance(message, Message):
        return message.model_dump(mode="json")
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        return cast(dict[str, Any], model_dump(mode="json"))
    if isinstance(message, dict):
        return cast(dict[str, Any], message)
    raise AttributionEvalError("attribution replay received an unsupported message")


def _tool_observation(messages: list[Message], call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        value = _message_value(message)
        if value.get("role") != "tool" or value.get("tool_call_id") != call_id:
            continue
        content = value.get("content")
        if not isinstance(content, str):
            break
        envelope = json.loads(content)
        if envelope.get("ok") is True and isinstance(envelope.get("data"), dict):
            return cast(dict[str, Any], envelope["data"])
        break
    raise AttributionEvalError("attribution replay tool observation is unavailable")


def _fixture_evidence_value(tool_name: str, data: dict[str, Any]) -> tuple[str, JsonValue]:
    try:
        if tool_name in {"query_funnel", "drill_down"}:
            path = "/rows/1/metrics/redemption_rate"
            value = data["rows"][1]["metrics"]["redemption_rate"]
        elif tool_name == "query_activity":
            path = "/activities/0/ends_on"
            value = data["activities"][0]["ends_on"]
        elif tool_name == "query_market_overview":
            path = "/segments/0/redemption_rate_change"
            value = data["segments"][0]["redemption_rate_change"]
        elif tool_name == "search_history_experience":
            path = "/query"
            value = data["query"]
        else:
            raise KeyError(tool_name)
    except (KeyError, IndexError, TypeError) as exc:
        raise AttributionEvalError("fixture evidence is unavailable for an expected tool") from exc
    return path, cast(JsonValue, value)


class _AttributionReplayProvider:
    def __init__(self, cases: tuple[AttributionGoldenCase, ...]) -> None:
        self._cases = {case.case_id: case for case in cases}
        self._turns: dict[str, int] = {}

    async def capabilities(self, ctx: Context) -> ProviderCapabilities:
        del ctx
        return ProviderCapabilities(
            tool_calling=True,
            streaming=True,
            reasoning=False,
            structured_output=True,
            parallel_tool_calls=False,
            structured_output_modes=frozenset({"native_json_schema"}),
            api_dialect="mock",
        )

    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        del tools, options
        case = self._cases[ctx.run_id]
        turn = self._turns.get(ctx.run_id, 0)
        self._turns[ctx.run_id] = turn + 1
        if turn < len(case.expected_tools):
            tool_name = case.expected_tools[turn]
            return ChatResult(
                content=(TextBlock(text="Inspecting deterministic attribution evidence."),),
                tool_calls=(
                    ToolCall(
                        id=f"{case.case_id}-tool-{turn}",
                        name=tool_name,
                        args=_tool_args(tool_name, case),
                    ),
                ),
                usage=Usage(input_tokens=1, output_tokens=1),
                finish_reason="tool_calls",
            )
        return ChatResult(
            content=(),
            tool_calls=(),
            structured_output=self._conclusion(case, messages),
            usage=Usage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
        )

    async def chat_stream(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        result = await self.chat(messages, ctx, tools, options)
        yield Done(
            sequence=0,
            provider="fixture",
            model="attribution-replay-v1",
            request_id=None,
            finish_reason=result.finish_reason,
        )

    @staticmethod
    def _conclusion(
        case: AttributionGoldenCase,
        messages: list[Message],
    ) -> dict[str, JsonValue]:
        if case.expected_outcome == "insufficient":
            return cast(
                dict[str, JsonValue],
                {
                    "schema_version": 1,
                    "outcome": "insufficient",
                    "conclusion": None,
                    "hypotheses": [],
                    "evidence": [],
                    "confidence": 0.2,
                    "confidence_explanation": "Fixture confidence is uncalibrated.",
                    "abstained": True,
                    "requested_data": list(case.requested_data),
                },
            )
        hypotheses: list[dict[str, JsonValue]] = [
            cast(
                dict[str, JsonValue],
                {
                    "hypothesis_id": f"h{index}",
                    "statement": statement,
                    "uncertainty": "Fixture wording is deterministic and not a quality judgment.",
                },
            )
            for index, statement in enumerate(case.acceptable_hypotheses, start=1)
        ]
        hypothesis_ids = [str(item["hypothesis_id"]) for item in hypotheses]
        evidence: list[dict[str, JsonValue]] = []
        for index, tool_name in enumerate(case.expected_tools):
            call_id = f"{case.case_id}-tool-{index}"
            data = _tool_observation(messages, call_id)
            data_path, value = _fixture_evidence_value(tool_name, data)
            evidence.append(
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "data_path": data_path,
                    "value": value,
                    "supports": cast(list[JsonValue], hypothesis_ids),
                }
            )
        return cast(
            dict[str, JsonValue],
            {
                "schema_version": 1,
                "outcome": case.expected_outcome,
                "conclusion": (
                    case.acceptable_hypotheses[0] if case.expected_outcome == "attributed" else None
                ),
                "hypotheses": hypotheses,
                "evidence": evidence,
                "confidence": 0.8 if case.expected_outcome == "attributed" else 0.45,
                "confidence_explanation": "Fixture confidence is uncalibrated.",
                "abstained": False,
                "requested_data": [],
            },
        )


def _principal(tenant_id: str, *, kind: Literal["human", "service"]) -> Principal:
    return Principal(
        subject_id="eval-reviewer" if kind == "human" else "eval-runner",
        tenant_id=tenant_id,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="trusted-eval-fixture",
    )


def build_attribution_eval_runtime(
    base: RuntimeServices,
    cases: tuple[AttributionGoldenCase, ...],
    query_database: Path,
    *,
    llm: LLMProvider | None = None,
    trusted_actors: tuple[Principal, ...] | None = None,
    trusted_executors: tuple[Principal, ...] | None = None,
) -> RuntimeServices:
    """Bind synthetic attribution tools to either the fixture or selected Live LLM."""

    actors = trusted_actors or tuple(
        _principal(tenant, kind="human") for tenant in sorted({c.tenant_id for c in cases})
    )
    executors = trusted_executors or tuple(
        _principal(tenant, kind="service") for tenant in sorted({c.tenant_id for c in cases})
    )
    if base.retriever is None:
        raise AttributionEvalError("attribution evaluation runtime has no retriever")
    stack = SealedAsyncExitStack()
    stack.seal()
    return RuntimeServices(
        config=base.config,
        policy=LocalPolicyEngine(trusted_actors=actors, trusted_executors=executors),
        domain=base.domain,
        tools=build_attribution_tool_registry(AnalyticsQueryStore(query_database), base.retriever),
        guardrails=base.guardrails,
        nodes=base.nodes,
        agents=base.agents,
        ingress=base.ingress,
        notifier=base.notifier,
        exit_stack=stack,
        llm=_AttributionReplayProvider(cases) if llm is None else llm,
        retriever=base.retriever,
        embedder=base.embedder,
        memory=base.memory,
        cache=base.cache,
        objects=base.objects,
        knowledge=base.knowledge,
        rule_snapshots=base.rule_snapshots,
    )


def _selected_cases(
    dataset: GoldenDataset,
    split: AttributionSplit,
) -> tuple[AttributionGoldenCase, ...]:
    cases = tuple(
        case
        for case in dataset.cases
        if isinstance(case, AttributionGoldenCase) and (split == "all" or case.split == split)
    )
    if not cases:
        raise AttributionEvalError("attribution evaluation selected no cases")
    return cases


async def run_attribution_eval(
    manifest_path: Path,
    *,
    rubric_path: Path,
    data_dir: Path,
    split: AttributionSplit = "all",
) -> AttributionEvalReport:
    """Run approved Scenario B cases through the real bounded graph offline."""

    dataset = load_golden_dataset(manifest_path)
    rubric = load_attribution_rubric(rubric_path)
    if rubric.dataset_version != dataset.manifest.dataset_version:
        raise AttributionEvalError("attribution dataset and rubric versions differ")
    rubric_payload = rubric_path.read_bytes()
    rubric_sha256 = hashlib.sha256(rubric_payload).hexdigest()
    if rubric_sha256 != dataset.manifest.rubric_sha256:
        raise AttributionEvalError("attribution rubric does not match the dataset manifest")
    cases = _selected_cases(dataset, split)
    query_databases: dict[str, Path] = {}
    for fixture_variant in sorted({case.fixture_variant for case in cases}):
        query_database = data_dir / "fixtures" / fixture_variant / "analytics.db"
        label_database = data_dir / "evaluation-only" / fixture_variant / "labels.db"
        generate_attribution_fixture(
            query_database,
            label_database,
            fixture_variant=fixture_variant,
        )
        query_databases[fixture_variant] = query_database
    config = resolve_runtime_config(
        environ={"ORIA_ENVIRONMENT": "test"}, data_dir=data_dir / "runtime"
    )
    await initialize_data(config)
    base = await build_runtime(config)
    runtime: RuntimeServices | None = None
    results: list[AttributionCaseResult] = []
    blind_items: list[AttributionBlindItem] = []
    tool_schema_versions: dict[str, int] = {}
    try:
        for case in cases:
            runtime = build_attribution_eval_runtime(
                base,
                (case,),
                query_databases[case.fixture_variant],
            )
            current_versions = {spec.name: spec.schema_version for spec in runtime.tools.specs()}
            if tool_schema_versions and current_versions != tool_schema_versions:
                raise AttributionEvalError("attribution fixture tool schemas differ by variant")
            tool_schema_versions = current_versions
            actor = _principal(case.tenant_id, kind="human")
            executor = _principal(case.tenant_id, kind="service")
            ctx = runtime.new_context(
                actor=actor,
                executor=executor,
                session_id="attribution-golden",
                thread_id=case.case_id,
                run_id=case.case_id,
            )
            state = await build_attribution_graph(checkpointer=InMemorySaver()).ainvoke(
                initial_attribution_state(
                    question=case.question,
                    analysis_period=_ANALYSIS_PERIOD,
                    conversation_history=case.conversation_history,
                ),
                config={"configurable": {"thread_id": case.case_id}},
                context=ResearchRunContext(ctx=ctx, limits=attribution_research_limits()),
            )
            case_result, blind_item = _evaluate_case(case, state, dataset.manifest.dataset_sha256)
            results.append(case_result)
            blind_items.append(blind_item)
            await runtime.aclose()
            runtime = None
    finally:
        if runtime is not None:
            await runtime.aclose()
        await base.aclose()
    metrics = _metrics(cases, tuple(results))
    fingerprint_value = {
        "runner_version": _RUNNER_VERSION,
        "dataset_version": dataset.manifest.dataset_version,
        "dataset_sha256": dataset.manifest.dataset_sha256,
        "rubric_sha256": rubric_sha256,
        "split": split,
        "tool_schema_versions": tool_schema_versions,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AttributionEvalReport(
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        rubric_version=rubric.rubric_version,
        rubric_sha256=rubric_sha256,
        split=split,
        tool_schema_versions=tool_schema_versions,
        eval_fingerprint=f"sha256:{fingerprint}",
        executed_at=datetime.now().astimezone(),
        cases=tuple(results),
        metrics=metrics,
        blind_review_items=tuple(blind_items),
    )


def _evaluate_case(
    case: AttributionGoldenCase,
    state: dict[str, Any],
    dataset_sha256: str,
) -> tuple[AttributionCaseResult, AttributionBlindItem]:
    conclusion = cast(dict[str, Any] | None, state.get("conclusion"))
    termination = cast(dict[str, Any] | None, state.get("termination"))
    if conclusion is None:
        outcome: Literal["attributed", "conflicting", "insufficient", "runtime_failure"] = (
            "runtime_failure"
        )
        abstained = None
        confidence = None
        hypotheses: tuple[dict[str, JsonValue], ...] = ()
        evidence: tuple[dict[str, JsonValue], ...] = ()
        requested_data: tuple[str, ...] = ()
        conclusion_text = None
    else:
        outcome = cast(Literal["attributed", "conflicting", "insufficient"], conclusion["outcome"])
        abstained = cast(bool, conclusion["abstained"])
        confidence = cast(float, conclusion["confidence"])
        hypotheses = tuple(cast(list[dict[str, JsonValue]], conclusion["hypotheses"]))
        evidence = tuple(cast(list[dict[str, JsonValue]], conclusion["evidence"]))
        requested_data = tuple(cast(list[str], conclusion["requested_data"]))
        conclusion_text = cast(str | None, conclusion["conclusion"])
    tool_results = cast(dict[str, dict[str, JsonValue]], state.get("tool_results", {}))
    executed_tools = tuple(cast(str, record["tool_name"]) for record in tool_results.values())
    expected_hypotheses = tuple(case.acceptable_hypotheses)
    observed_hypotheses = tuple(cast(str, hypothesis["statement"]) for hypothesis in hypotheses)
    hypothesis_match = sorted(expected_hypotheses) == sorted(observed_hypotheses)
    evidence_grounded = None if case.expected_outcome == "insufficient" else termination is None
    failures: list[str] = []
    if outcome != case.expected_outcome:
        failures.append("outcome_mismatch")
    if abstained != case.expected_abstain:
        failures.append("abstain_mismatch")
    if not set(case.expected_tools).issubset(executed_tools):
        failures.append("required_tool_missing")
    if set(case.forbidden_tools).intersection(executed_tools):
        failures.append("forbidden_tool_executed")
    if not hypothesis_match:
        failures.append("hypothesis_mismatch")
    if case.expected_outcome != "insufficient" and evidence_grounded is not True:
        failures.append("evidence_not_grounded")
    if termination is not None:
        failures.append("runtime_termination")
    blind_id = hashlib.sha256(f"{dataset_sha256}:{case.case_id}".encode()).hexdigest()[:16]
    return (
        AttributionCaseResult(
            case_id=case.case_id,
            split=case.split,
            critical=case.critical,
            passed=not failures,
            observed_outcome=outcome,
            termination_reason=(
                None if termination is None else cast(str, termination.get("reason"))
            ),
            abstained=abstained,
            executed_tools=executed_tools,
            evidence_grounded=evidence_grounded,
            hypothesis_match=hypothesis_match,
            confidence=confidence,
            failures=tuple(failures),
        ),
        AttributionBlindItem(
            blind_case_id=f"blind_{blind_id}",
            conversation_history=case.conversation_history,
            question=case.question,
            observed_outcome=outcome,
            conclusion=conclusion_text,
            hypotheses=hypotheses,
            evidence=evidence,
            requested_data=requested_data,
        ),
    )


def _metrics(
    cases: tuple[AttributionGoldenCase, ...],
    results: tuple[AttributionCaseResult, ...],
) -> AttributionMetrics:
    expected = {case.case_id: case for case in cases}
    critical = [result for result in results if result.critical]
    attributed = [
        result for result in results if expected[result.case_id].expected_outcome != "insufficient"
    ]
    count = len(results)
    return AttributionMetrics(
        evaluated_cases=count,
        case_pass_rate=sum(result.passed for result in results) / count,
        critical_pass_rate=sum(result.passed for result in critical) / len(critical),
        outcome_accuracy=sum(
            result.observed_outcome == expected[result.case_id].expected_outcome
            for result in results
        )
        / count,
        abstain_accuracy=sum(
            result.abstained == expected[result.case_id].expected_abstain for result in results
        )
        / count,
        required_tool_coverage=sum(
            set(expected[result.case_id].expected_tools).issubset(result.executed_tools)
            for result in results
        )
        / count,
        forbidden_tool_safety_rate=sum(
            not set(expected[result.case_id].forbidden_tools).intersection(result.executed_tools)
            for result in results
        )
        / count,
        grounded_evidence_rate=sum(result.evidence_grounded is True for result in attributed)
        / len(attributed),
    )
