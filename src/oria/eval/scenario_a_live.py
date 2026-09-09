"""Opt-in Scenario A evaluation using unmodified runtime services."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from oria.agent import ResearchRunContext, build_research_graph, initial_research_state
from oria.config import resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.runtime import build_runtime
from oria.core.types import ValueModel
from oria.data import initialize_data
from oria.eval.datasets import GoldenCase, load_golden_dataset
from oria.eval.scenario_a import (
    _EFFECTIVE_AT,
    ScenarioACaseResult,
    ScenarioAMetrics,
    _evaluate_case,
    _metrics,
)
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document


class ScenarioALiveCase(ScenarioACaseResult):
    """Keep the shared scorer's diagnostics and expose Live report names."""

    automated_pass: bool
    outcome: Literal["proposal", "abstain", "runtime_failure"]
    tool_sequence: tuple[str, ...]
    grounded: bool | None


class ScenarioALiveUsage(ValueModel):
    status: Literal["graph_reported"] = "graph_reported"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: None = None
    reason: str = "Tokens reported by graph; cost unavailable without a pricing snapshot"


class ScenarioALiveReport(ValueModel):
    schema_version: Literal[1] = 1
    suite: Literal["scenario_a"] = "scenario_a"
    verification_level: Literal["live"] = "live"
    runner_version: Literal["scenario_a_live_v1"] = "scenario_a_live_v1"
    dataset_version: str
    dataset_sha256: str
    dataset_case_count: int
    target_id: str
    provider: str
    model: str
    config_fingerprint: str
    status: Literal["completed", "failed", "in_progress"]
    cases: tuple[ScenarioALiveCase, ...] = ()
    metrics: ScenarioAMetrics | None = None
    usage: ScenarioALiveUsage = ScenarioALiveUsage()
    error_type: str | None = None
    fixture_policy: Literal["real_runtime_no_fixture_injection"] = (
        "real_runtime_no_fixture_injection"
    )


def resolve_scenario_a_live_target(
    target: str, *, data_dir: Path, environ: Mapping[str, str]
) -> ResolvedRuntimeConfig:
    """Require explicit opt-in and let the config resolver validate profile names."""
    if environ.get("ORIA_RUN_LIVE") != "1":
        raise ValueError("Scenario A Live requires ORIA_RUN_LIVE=1")
    if not target.strip():
        raise ValueError("Scenario A Live requires a nonempty target")
    config = resolve_runtime_config(
        runtime_profile="standard",
        llm_profile=target,
        embedding_profile="fixture",
        data_dir=data_dir,
        environ={**environ, "ORIA_ENVIRONMENT": "test"},
    )
    if config.llm.provider == "mock":
        raise ValueError("Scenario A Live requires a non-mock target")
    return config


async def run_scenario_a_live(
    manifest_path: Path,
    *,
    target: str,
    data_dir: Path,
    environ: Mapping[str, str],
    max_new_case_runs: int | None = None,
    checkpoint: Callable[[ScenarioALiveReport], None] | None = None,
) -> ScenarioALiveReport:
    """Run real LLM, tools, knowledge and policy; score without Golden gates.

    A bounded run starts from the first case; this initial version does not resume.
    Completed records are checkpointed after every case, including on failure.
    """
    if max_new_case_runs is not None and max_new_case_runs < 1:
        raise ValueError("max_new_case_runs must be positive")
    config = resolve_scenario_a_live_target(target, data_dir=data_dir, environ=environ)
    dataset = load_golden_dataset(manifest_path)
    if dataset.manifest.suite != "scenario_a":
        raise ValueError("Scenario A Live requires a Scenario A dataset")
    report = ScenarioALiveReport(
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        dataset_case_count=len(dataset.cases),
        target_id=target,
        provider=config.llm.provider,
        model=config.llm.model,
        config_fingerprint=config.config_fingerprint,
        status="in_progress",
    )
    results: list[ScenarioALiveCase] = []
    try:
        await initialize_data(config)
        runtime = await build_runtime(config)
        async with runtime:
            ingest_ctx = runtime.new_context(
                actor=local_operator(),
                executor=local_cli_executor(),
                session_id="scenario-a-live",
                thread_id="ingest",
                run_id="ingest",
            )
            if runtime.knowledge is None:
                raise ValueError("Scenario A Live requires knowledge service")
            await runtime.knowledge.ingest(demo_rule_document(), ingest_ctx)
            for case in dataset.cases:
                assert isinstance(case, GoldenCase)
                ctx = runtime.new_context(
                    actor=local_operator(),
                    executor=local_cli_executor(),
                    session_id="scenario-a-live",
                    thread_id=case.case_id,
                    run_id=case.case_id,
                )
                graph = build_research_graph(checkpointer=InMemorySaver())
                state = await graph.ainvoke(
                    initial_research_state(user_request=case.input, effective_at=_EFFECTIVE_AT),
                    config={"configurable": {"thread_id": case.case_id}},
                    context=ResearchRunContext(ctx=ctx),
                )
                scored = await _evaluate_case(case, state, ctx)
                results.append(
                    ScenarioALiveCase(
                        **scored.model_dump(),
                        automated_pass=scored.passed,
                        outcome=scored.observed_outcome,
                        tool_sequence=scored.executed_tools,
                        grounded=scored.citations_valid,
                    )
                )
                report = report.model_copy(
                    update={
                        "cases": tuple(results),
                        "metrics": _metrics(dataset, tuple(results)),
                        "usage": ScenarioALiveUsage(
                            input_tokens=report.usage.input_tokens + state["input_tokens"],
                            output_tokens=report.usage.output_tokens + state["output_tokens"],
                        ),
                    }
                )
                if checkpoint is not None:
                    checkpoint(report)
                if max_new_case_runs is not None and len(results) >= max_new_case_runs:
                    break
        report = report.model_copy(
            update={
                "status": "completed" if len(results) == len(dataset.cases) else "in_progress",
            }
        )
    except Exception as exc:
        # Never put provider exception messages, credentials or prompts in the report.
        report = report.model_copy(update={"status": "failed", "error_type": type(exc).__name__})
    if checkpoint is not None:
        checkpoint(report)
    return report
