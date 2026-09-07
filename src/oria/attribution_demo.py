"""Application service for the bounded Scenario B attribution demonstration."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from oria.agent import (
    AttributionConclusion,
    ResearchLimits,
    ResearchRunContext,
    attribution_research_limits,
    build_attribution_graph,
    initial_attribution_state,
)
from oria.agent.models import AgentTermination
from oria.analytics.demo import attribution_history_document
from oria.config import resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.runtime import build_runtime
from oria.core.types import JsonValue, Principal, ToolResult, ValueModel
from oria.data import initialize_data
from oria.eval.attribution import build_attribution_eval_runtime
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset

_DEFAULT_CASE_ID = "sb-v1-001"
_ANALYSIS_PERIOD = "2026-08-18/2026-09-01"
AttributionMatchMethod = Literal["default_case_id", "case_id_exact", "normalized_question_exact"]


class AttributionAskError(RuntimeError):
    """Safe application-boundary failure for ``attribution ask``."""

    def __init__(self, code: str, detail: str, *, exit_code: int = 1) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code


class AttributionToolTrace(ValueModel):
    sequence: int = Field(ge=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    result: ToolResult


class AttributionAskUsage(ValueModel):
    model_turns: int = Field(ge=0)
    tool_calls_total: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_cost: float = Field(ge=0)


class AttributionAskResult(ValueModel):
    ok: Literal[True] = True
    schema_version: Literal[1] = 1
    executed_at: datetime
    mode: Literal["mock_replay", "live"]
    environment_source: Literal["clean_mock", "merged_live"]
    runtime_environment: Literal["development", "test", "production"]
    profile: str
    provider: str
    model: str
    config_fingerprint: str
    case_id: str
    split: Literal["development"] = "development"
    user_question: str | None
    question_asked: str
    match_method: AttributionMatchMethod
    tenant_id: str
    session_id: str
    thread_id: str
    run_id: str
    correlation_id: str
    analysis_period: str
    limits: ResearchLimits
    tool_trace: tuple[AttributionToolTrace, ...]
    conclusion: AttributionConclusion | None = None
    termination: AgentTermination | None = None
    usage: AttributionAskUsage
    fixture_dir: str
    report_path: str


def _normalize_question(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _select_case(
    manifest_path: Path,
    *,
    case_id: str | None,
    question: str | None,
) -> tuple[AttributionGoldenCase, AttributionMatchMethod, str | None]:
    dataset = load_golden_dataset(manifest_path)
    cases = tuple(case for case in dataset.cases if isinstance(case, AttributionGoldenCase))
    by_id = {case.case_id: case for case in cases}

    if question is not None:
        if case_id is not None:
            raise AttributionAskError(
                "ambiguous_selection",
                "free-form question and --case-id cannot be used together",
                exit_code=2,
            )
        normalized = _normalize_question(question)
        matches = tuple(case for case in cases if _normalize_question(case.question) == normalized)
        if not matches:
            raise AttributionAskError(
                "unknown_question",
                "the question did not exactly match a reviewed Scenario B case",
                exit_code=2,
            )
        if len(matches) != 1:
            raise AttributionAskError(
                "ambiguous_question",
                "the normalized question matched more than one Scenario B case",
                exit_code=2,
            )
        selected = matches[0]
        method: AttributionMatchMethod = "normalized_question_exact"
        user_question = question
    else:
        selected_id = case_id or _DEFAULT_CASE_ID
        candidate = by_id.get(selected_id)
        if candidate is None:
            raise AttributionAskError(
                "unknown_case",
                f"Scenario B case is unknown: {selected_id}",
                exit_code=2,
            )
        selected = candidate
        method = "case_id_exact" if case_id is not None else "default_case_id"
        user_question = None

    if selected.split != "development":
        raise AttributionAskError(
            "holdout_forbidden",
            "attribution ask only permits reviewed development cases; holdout is unavailable",
            exit_code=2,
        )
    return selected, method, user_question


def _reserve_run_directory(data_dir: Path, case_id: str) -> Path:
    reports_root = data_dir.resolve(strict=False) / "reports-tmp"
    reports_root.mkdir(parents=True, exist_ok=True)
    if reports_root.is_symlink():
        raise AttributionAskError("unsafe_output_path", "reports-tmp cannot be a symbolic link")
    attribution_root = reports_root / "attribution"
    attribution_root.mkdir(parents=False, exist_ok=True)
    if attribution_root.is_symlink():
        raise AttributionAskError(
            "unsafe_output_path", "the attribution report directory cannot be a symbolic link"
        )
    case_root = attribution_root / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    if case_root.is_symlink():
        raise AttributionAskError(
            "unsafe_output_path", "the attribution case directory cannot be a symbolic link"
        )
    run_dir = case_root / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=False, exist_ok=False)
    if reports_root not in run_dir.resolve(strict=False).parents:
        raise AttributionAskError("unsafe_output_path", "attribution output escaped reports-tmp")
    return run_dir


def _resolve_config(
    *,
    run_dir: Path,
    llm_profile: str | None,
) -> ResolvedRuntimeConfig:
    runtime_dir = run_dir / "runtime"
    if llm_profile is None:
        clean_config = run_dir / "clean-mock-config.yaml"
        clean_config.write_text("{}\n", encoding="utf-8")
        return resolve_runtime_config(
            config_path=clean_config,
            runtime_profile="demo",
            llm_profile="mock",
            embedding_profile="fixture",
            data_dir=runtime_dir,
            environ={},
        )
    config = resolve_runtime_config(
        llm_profile=llm_profile,
        data_dir=runtime_dir,
        environ=None,
    )
    if config.llm.provider == "mock":
        raise AttributionAskError(
            "live_profile_required",
            "an explicit --llm-profile must resolve to a non-Mock provider",
            exit_code=2,
        )
    return config


def _demo_principals(tenant_id: str) -> tuple[Principal, Principal]:
    return (
        Principal(
            subject_id="attribution-demo-operator",
            tenant_id=tenant_id,
            kind="human",
            roles=("operator",),
            authn_method="trusted-attribution-demo",
        ),
        Principal(
            subject_id="attribution-demo-cli",
            tenant_id=tenant_id,
            kind="service",
            roles=("runtime",),
            authn_method="trusted-attribution-demo",
        ),
    )


def _tool_trace(state: dict[str, Any]) -> tuple[AttributionToolTrace, ...]:
    raw_results = state.get("tool_results")
    if not isinstance(raw_results, dict):
        raise AttributionAskError("invalid_graph_result", "tool results are unavailable")
    trace: list[AttributionToolTrace] = []
    for call_id, raw_record in raw_results.items():
        if not isinstance(call_id, str) or not isinstance(raw_record, dict):
            raise AttributionAskError("invalid_graph_result", "tool trace is malformed")
        tool_name = raw_record.get("tool_name")
        arguments = raw_record.get("arguments")
        result = raw_record.get("result")
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            raise AttributionAskError("invalid_graph_result", "tool trace is malformed")
        trace.append(
            AttributionToolTrace(
                sequence=len(trace) + 1,
                tool_call_id=call_id,
                tool_name=tool_name,
                arguments=cast(dict[str, JsonValue], arguments),
                result=ToolResult.model_validate(result),
            )
        )
    return tuple(trace)


def _write_report(path: Path, result: AttributionAskResult, *, run_dir: Path) -> None:
    if path.parent.resolve(strict=False) != run_dir.resolve(strict=False) or path.name != (
        "attribution.json"
    ):
        raise AttributionAskError("unsafe_output_path", "attribution report path is unsafe")
    payload = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=run_dir, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


async def run_attribution_ask(
    manifest_path: Path,
    *,
    data_dir: Path,
    case_id: str | None = None,
    question: str | None = None,
    llm_profile: str | None = None,
) -> AttributionAskResult:
    """Run one reviewed development case through the real bounded attribution graph."""

    selected, match_method, user_question = _select_case(
        manifest_path,
        case_id=case_id,
        question=question,
    )
    run_dir = _reserve_run_directory(data_dir, selected.case_id)
    config = _resolve_config(run_dir=run_dir, llm_profile=llm_profile)
    fixture_dir = run_dir / "fixture" / selected.fixture_variant
    query_database = fixture_dir / "analytics.db"
    label_database = fixture_dir / "evaluation-only" / "labels.db"
    try:
        generate_attribution_fixture(
            query_database,
            label_database,
            fixture_variant=selected.fixture_variant,
        )
        await initialize_data(config)
    except Exception as exc:
        raise AttributionAskError(
            "fixture_setup_failed",
            "the isolated Scenario B fixture could not be initialized",
        ) from exc

    base = None
    runtime = None
    token = uuid.uuid4().hex
    actor, executor = _demo_principals(selected.tenant_id)
    limits = attribution_research_limits()
    try:
        base = await build_runtime(config)
        runtime = build_attribution_eval_runtime(
            base,
            (selected,),
            query_database,
            llm=None if llm_profile is None else base.llm,
            trusted_actors=(actor,),
            trusted_executors=(executor,),
        )
        ctx = runtime.new_context(
            actor=actor,
            executor=executor,
            session_id=f"attribution-session-{token}",
            thread_id=f"attribution-thread-{token}",
            run_id=selected.case_id,
            correlation_id=f"attribution-correlation-{token}",
        )
        await ctx.knowledge.ingest(attribution_history_document(), ctx)
        state = await build_attribution_graph(checkpointer=InMemorySaver()).ainvoke(
            initial_attribution_state(
                question=selected.question,
                analysis_period=_ANALYSIS_PERIOD,
                conversation_history=selected.conversation_history,
            ),
            config={"configurable": {"thread_id": ctx.thread_id}},
            context=ResearchRunContext(ctx=ctx, limits=limits),
        )
        raw_conclusion = state.get("conclusion")
        raw_termination = state.get("termination")
        conclusion = (
            AttributionConclusion.model_validate(raw_conclusion)
            if isinstance(raw_conclusion, dict)
            else None
        )
        termination = (
            AgentTermination.model_validate(raw_termination)
            if isinstance(raw_termination, dict)
            else None
        )
        if (conclusion is None) == (termination is None):
            raise AttributionAskError(
                "invalid_graph_result",
                "the attribution graph returned neither one conclusion nor one termination",
            )
        input_tokens = int(state["input_tokens"])
        output_tokens = int(state["output_tokens"])
        report_path = run_dir / "attribution.json"
        result = AttributionAskResult(
            executed_at=datetime.now(UTC),
            mode="mock_replay" if llm_profile is None else "live",
            environment_source="clean_mock" if llm_profile is None else "merged_live",
            runtime_environment=config.environment,
            profile=config.llm.profile_id,
            provider=config.llm.provider,
            model=config.llm.model,
            config_fingerprint=config.config_fingerprint,
            case_id=selected.case_id,
            user_question=user_question,
            question_asked=selected.question,
            match_method=match_method,
            tenant_id=selected.tenant_id,
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
            run_id=ctx.run_id,
            correlation_id=ctx.correlation_id,
            analysis_period=_ANALYSIS_PERIOD,
            limits=limits,
            tool_trace=_tool_trace(cast(dict[str, Any], state)),
            conclusion=conclusion,
            termination=termination,
            usage=AttributionAskUsage(
                model_turns=int(state["model_turns"]),
                tool_calls_total=int(state["tool_calls_total"]),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                total_cost=float(state["total_cost"]),
            ),
            fixture_dir=str(fixture_dir),
            report_path=str(report_path),
        )
        _write_report(report_path, result, run_dir=run_dir)
        return result
    except AttributionAskError:
        raise
    except Exception as exc:
        raise AttributionAskError(
            "attribution_execution_failed",
            "the bounded attribution run failed before producing a safe result",
        ) from exc
    finally:
        if runtime is not None:
            await runtime.aclose()
        if base is not None:
            await base.aclose()
