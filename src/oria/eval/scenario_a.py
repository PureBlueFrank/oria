"""Deterministic Scenario A Golden runner, baseline, and regression gates."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, model_validator

from oria.agent import ResearchRunContext, build_research_graph, initial_research_state
from oria.config import resolve_runtime_config
from oria.core.context import Context, RuntimeServices, SealedAsyncExitStack
from oria.core.protocols import PolicyEngine, Tool
from oria.core.runtime import build_runtime
from oria.core.types import (
    AuthorizationRequest,
    ChatOptions,
    ChatResult,
    CitationBlock,
    Done,
    JsonValue,
    Message,
    PolicyDecision,
    ProviderCapabilities,
    StreamEvent,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    ValueModel,
)
from oria.data import initialize_data
from oria.eval.datasets import GoldenCase, GoldenDataset, load_golden_dataset
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.tools.models import SearchCampaignRulesParams, SearchCampaignRulesResult
from oria.tools.registry import ToolRegistry

_RUNNER_VERSION: Literal["scenario_a_v1"] = "scenario_a_v1"
_EFFECTIVE_AT = "2026-07-15T00:00:00+08:00"
_METRIC_NAMES = frozenset(
    {
        "case_pass_rate",
        "critical_pass_rate",
        "outcome_accuracy",
        "tool_sequence_accuracy",
        "grounded_proposal_rate",
    }
)


class ScenarioACaseResult(ValueModel):
    case_id: str
    critical: bool
    passed: bool
    observed_outcome: Literal["proposal", "abstain", "runtime_failure"]
    termination_reason: str | None = None
    executed_tools: tuple[str, ...]
    eligible_ids: tuple[str, ...] = ()
    recommended_ids: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    citations_valid: bool | None = None
    failures: tuple[str, ...] = ()


class ScenarioAMetrics(ValueModel):
    case_pass_rate: float = Field(ge=0, le=1)
    critical_pass_rate: float = Field(ge=0, le=1)
    outcome_accuracy: float = Field(ge=0, le=1)
    tool_sequence_accuracy: float = Field(ge=0, le=1)
    grounded_proposal_rate: float = Field(ge=0, le=1)


class ScenarioAReport(ValueModel):
    suite: Literal["scenario_a"] = "scenario_a"
    dataset_version: str
    dataset_sha256: str
    runner_version: Literal["scenario_a_v1"] = _RUNNER_VERSION
    tool_schema_versions: dict[str, int]
    cases: tuple[ScenarioACaseResult, ...]
    metrics: ScenarioAMetrics


class ScenarioABaseline(ValueModel):
    suite: Literal["scenario_a"] = "scenario_a"
    dataset_version: str
    dataset_sha256: str
    runner_version: Literal["scenario_a_v1"] = _RUNNER_VERSION
    prompt_version: Literal["merchant_selection/v1"] = "merchant_selection/v1"
    provider_profile: Literal["scenario_a_replay_v1"] = "scenario_a_replay_v1"
    embedding_profile: Literal["fixture"] = "fixture"
    tool_schema_versions: dict[str, int]
    created_at: datetime
    cases: tuple[ScenarioACaseResult, ...]
    metrics: ScenarioAMetrics


class ScenarioAGates(ValueModel):
    suite: Literal["scenario_a"]
    dataset_version: str
    allowed_regression: Literal[0] = 0
    required_metrics: dict[str, float]

    @model_validator(mode="after")
    def validate_metric_names(self) -> ScenarioAGates:
        if set(self.required_metrics) != _METRIC_NAMES:
            raise ValueError("Scenario A gates must configure every registered metric")
        if any(value < 0 or value > 1 for value in self.required_metrics.values()):
            raise ValueError("Scenario A gate metrics must be between zero and one")
        return self


class GoldenGateError(RuntimeError):
    """Raised when a deterministic Golden report regresses or violates its gate."""


class _ScenarioAReplayProvider:
    def __init__(self, cases: tuple[GoldenCase, ...]) -> None:
        self._cases = {case.case_id: case for case in cases}
        self._turns: dict[str, int] = {}

    async def capabilities(self, ctx: Context) -> ProviderCapabilities:
        del ctx
        return ProviderCapabilities(
            tool_calling=True,
            streaming=True,
            reasoning=False,
            structured_output=True,
            parallel_tool_calls=True,
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
        del options
        case = self._cases[ctx.run_id]
        turn = self._turns.get(ctx.run_id, 0)
        self._turns[ctx.run_id] = turn + 1
        variant = case.fixture_variant

        if variant == "prompt_injection:write_tool":
            return _tool_result(ToolCall(id="write", name="persist_campaign", args={}))
        if variant == "invalid_tool_arguments:rule_snapshot_id":
            return _tool_result(
                ToolCall(
                    id="invalid-snapshot",
                    name="query_merchants",
                    args={"rule_snapshot_id": "forged", "limit": 10},
                )
            )
        if variant == "duplicate_evidence":
            return _tool_result(_search_call(f"duplicate-{turn}"))
        if turn == 0:
            return _tool_result(_search_call("rules"))

        search = _tool_data(messages, "rules")
        if variant.startswith(("missing_rule_category:", "conflicting_rule_category:")):
            unresolved = tuple(cast(list[str], search["unresolved_items"]))
            return _structured_result(
                {
                    "schema_version": 1,
                    "unresolved_items": list(unresolved),
                    "abstained": True,
                }
            )
        if variant == "permission_denied:search_campaign_rules":
            raise AssertionError("denied rule search must terminate before a second model turn")
        if turn == 1:
            return _tool_result(
                ToolCall(
                    id="merchants",
                    name="query_merchants",
                    args={"rule_snapshot_id": search["rule_snapshot_id"], "limit": 10},
                )
            )
        if variant == "permission_denied:query_merchants":
            raise AssertionError("denied merchant query must terminate before a third model turn")

        assert tools is not None and {tool.name for tool in tools} == {
            "search_campaign_rules",
            "query_merchants",
        }
        merchants = _tool_data(messages, "merchants")
        proposal = _proposal(search, merchants)
        if case.output_mutation is not None:
            forged_id = case.output_mutation.get("append_merchant_id")
            if isinstance(forged_id, str):
                recommendations = cast(
                    list[dict[str, JsonValue]], proposal["recommended_merchants"]
                )
                recommendations.append(
                    {
                        "merchant_id": forged_id,
                        "rank": len(recommendations) + 1,
                        "reason": "fixture-forged candidate",
                    }
                )
        if variant == "forged_citation":
            proposal["field_evidence"] = {
                "basic.campaign_type": {
                    "type": "citation",
                    "document_id": "forged-document",
                    "document_version": "forged-version",
                    "chunk_id": "forged-chunk",
                }
            }
        return _structured_result(proposal)

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
            model="scenario-a-replay-v1",
            request_id=None,
            finish_reason=result.finish_reason,
        )


class _ScenarioSearchTool:
    def __init__(self, base: Tool, cases: dict[str, GoldenCase]) -> None:
        self._base = base
        self._cases = cases
        self.name = base.name
        self.schema_version = base.schema_version
        self.description = base.description
        self.json_schema = base.json_schema
        self.result_schema = base.result_schema
        self.policy = base.policy

    def validate_params(self, params: dict[str, Any]) -> None:
        self._base.validate_params(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        variant = self._cases[ctx.run_id].fixture_variant
        prefix, separator, category = variant.partition(":")
        if separator and prefix in {"missing_rule_category", "conflicting_rule_category"}:
            request = SearchCampaignRulesParams.model_validate(params)
            reason = "missing" if prefix == "missing_rule_category" else "conflict"
            data = SearchCampaignRulesResult(
                effective_at=request.effective_at,
                unresolved_items=(f"{reason}:{category}",),
            )
            return ToolResult(
                ok=True,
                data=data.model_dump(mode="json"),
                execution_id=f"golden_{ctx.run_id}",
                trust_level="trusted_internal",
                provenance="oria://eval/scenario_a/search_campaign_rules/v1",
                data_classification="restricted_derivative",
            )
        return await self._base.run(params, ctx)


class _ScenarioPolicy:
    def __init__(self, base: PolicyEngine, cases: dict[str, GoldenCase]) -> None:
        self._base = base
        self._cases = cases

    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision:
        variant = self._cases[ctx.run_id].fixture_variant
        denied_action = {
            "permission_denied:search_campaign_rules": "rule:read",
            "permission_denied:query_merchants": "merchant:read",
        }.get(variant)
        if request.action == denied_action:
            return PolicyDecision(
                allow=False,
                constraints={},
                policy_version="golden-deny-v1",
                reason="denied by Scenario A Golden fixture",
            )
        return await self._base.authorize(request, ctx)


def _tool_result(call: ToolCall) -> ChatResult:
    return ChatResult(
        content=(TextBlock(text="fixture tool request"),),
        tool_calls=(call,),
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="tool_calls",
    )


def _structured_result(value: dict[str, JsonValue]) -> ChatResult:
    return ChatResult(
        content=(),
        tool_calls=(),
        structured_output=value,
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="stop",
    )


def _search_call(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="search_campaign_rules",
        args={"intent": "merchant_recruitment", "effective_at": _EFFECTIVE_AT},
    )


def _tool_data(messages: list[Message], call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        if message.role != "tool" or message.tool_call_id != call_id:
            continue
        if not isinstance(message.content, str):
            raise AssertionError("tool observation must be canonical JSON")
        envelope = json.loads(message.content)
        if envelope.get("ok") is not True or not isinstance(envelope.get("data"), dict):
            raise AssertionError(f"tool observation failed: {call_id}")
        return cast(dict[str, Any], envelope["data"])
    raise AssertionError(f"tool observation is missing: {call_id}")


def _proposal(search: dict[str, Any], merchants: dict[str, Any]) -> dict[str, JsonValue]:
    del search
    candidates = cast(list[dict[str, Any]], merchants["candidates"])
    return {
        "schema_version": 1,
        "recommended_merchants": [
            {
                "merchant_id": candidate["merchant_id"],
                "rank": rank,
                "reason": "fixture verified hard eligibility",
            }
            for rank, candidate in enumerate(candidates, start=1)
        ],
        "unresolved_items": [],
        "abstained": False,
    }


def _eval_runtime(base: RuntimeServices, dataset: GoldenDataset) -> RuntimeServices:
    cases = {case.case_id: case for case in dataset.cases if isinstance(case, GoldenCase)}
    cases_tuple = tuple(c for c in dataset.cases if isinstance(c, GoldenCase))
    tools = ToolRegistry(allowlist=frozenset({"search_campaign_rules", "query_merchants"}))
    tools.register(_ScenarioSearchTool(base.tools.get("search_campaign_rules"), cases))
    tools.register(base.tools.get("query_merchants"))
    tools.seal()
    stack = SealedAsyncExitStack()
    stack.seal()
    return RuntimeServices(
        config=base.config,
        policy=_ScenarioPolicy(base.policy, cases),
        domain=base.domain,
        tools=tools,
        guardrails=base.guardrails,
        nodes=base.nodes,
        agents=base.agents,
        ingress=base.ingress,
        notifier=base.notifier,
        exit_stack=stack,
        llm=_ScenarioAReplayProvider(cases_tuple),
        retriever=base.retriever,
        embedder=base.embedder,
        memory=base.memory,
        cache=base.cache,
        objects=base.objects,
        knowledge=base.knowledge,
        rule_snapshots=base.rule_snapshots,
    )


async def run_scenario_a_golden(
    manifest_path: Path,
    *,
    data_dir: Path,
) -> ScenarioAReport:
    """Run every approved Golden case through the real bounded graph offline."""

    dataset = load_golden_dataset(manifest_path)
    config = resolve_runtime_config(environ={}, data_dir=data_dir)
    await initialize_data(config)
    base = await build_runtime(config)
    runtime: RuntimeServices | None = None
    tool_schema_versions: dict[str, int] = {}
    try:
        runtime = _eval_runtime(base, dataset)
        tool_schema_versions = {spec.name: spec.schema_version for spec in runtime.tools.specs()}
        ingest_ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="golden-ingest",
            thread_id="golden-ingest",
            run_id=dataset.cases[0].case_id,
        )
        if runtime.knowledge is None:
            raise GoldenGateError("Scenario A Golden runtime has no knowledge service")
        await runtime.knowledge.ingest(demo_rule_document(), ingest_ctx)
        results: list[ScenarioACaseResult] = []
        for case in dataset.cases:
            assert isinstance(case, GoldenCase)
            ctx = runtime.new_context(
                actor=local_operator(),
                executor=local_cli_executor(),
                session_id="scenario-a-golden",
                thread_id=case.case_id,
                run_id=case.case_id,
            )
            graph = build_research_graph(checkpointer=InMemorySaver())
            state = await graph.ainvoke(
                initial_research_state(
                    user_request=case.input,
                    effective_at=_EFFECTIVE_AT,
                ),
                config={"configurable": {"thread_id": case.case_id}},
                context=ResearchRunContext(ctx=ctx),
            )
            results.append(await _evaluate_case(case, state, ctx))
    finally:
        if runtime is not None:
            await runtime.aclose()
        await base.aclose()
    metrics = _metrics(dataset, tuple(results))
    return ScenarioAReport(
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        tool_schema_versions=tool_schema_versions,
        cases=tuple(results),
        metrics=metrics,
    )


async def _evaluate_case(
    case: GoldenCase,
    state: dict[str, Any],
    ctx: Context,
) -> ScenarioACaseResult:
    proposal = cast(dict[str, Any] | None, state.get("proposal"))
    termination = cast(dict[str, Any] | None, state.get("termination"))
    if proposal is not None and proposal.get("abstained") is True:
        outcome: Literal["proposal", "abstain", "runtime_failure"] = "abstain"
    elif proposal is not None:
        outcome = "proposal"
    else:
        outcome = "runtime_failure"
    reason = None if termination is None else cast(str, termination.get("reason"))
    tools = _executed_tools(cast(list[dict[str, Any]], state["messages"]))
    merchant_result = cast(dict[str, Any] | None, state.get("merchant_result"))
    eligible_ids: tuple[str, ...] = ()
    if merchant_result is not None:
        eligible_ids = tuple(
            cast(str, item["merchant_id"])
            for item in cast(list[dict[str, Any]], merchant_result["candidates"])
        )
    recommended_ids: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    citations_valid: bool | None = None
    if proposal is not None:
        recommended_ids = tuple(
            cast(str, item["merchant_id"])
            for item in cast(list[dict[str, Any]], proposal["recommended_merchants"])
        )
        unresolved_items = tuple(cast(list[str], proposal["unresolved_items"]))
        if outcome == "proposal":
            evidence = cast(dict[str, dict[str, JsonValue]], proposal["field_evidence"])
            citations_valid = all(
                [
                    await ctx.knowledge.citation_exists(CitationBlock.model_validate(value), ctx)
                    for value in evidence.values()
                ]
            )
    failures = _case_failures(
        case,
        outcome=outcome,
        reason=reason,
        tools=tools,
        eligible_ids=eligible_ids,
        recommended_ids=recommended_ids,
        unresolved_items=unresolved_items,
        citations_valid=citations_valid,
        rule_result=cast(dict[str, Any] | None, state.get("rule_result")),
    )
    return ScenarioACaseResult(
        case_id=case.case_id,
        critical=case.critical,
        passed=not failures,
        observed_outcome=outcome,
        termination_reason=reason,
        executed_tools=tools,
        eligible_ids=eligible_ids,
        recommended_ids=recommended_ids,
        unresolved_items=unresolved_items,
        citations_valid=citations_valid,
        failures=failures,
    )


def _executed_tools(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    names_by_id: dict[str, str] = {}
    executed: list[str] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_call":
                    call_id, name = block.get("id"), block.get("name")
                    if isinstance(call_id, str) and isinstance(name, str):
                        names_by_id[call_id] = name
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in names_by_id:
                executed.append(names_by_id[call_id])
    return tuple(executed)


def _case_failures(
    case: GoldenCase,
    *,
    outcome: str,
    reason: str | None,
    tools: tuple[str, ...],
    eligible_ids: tuple[str, ...],
    recommended_ids: tuple[str, ...],
    unresolved_items: tuple[str, ...],
    citations_valid: bool | None,
    rule_result: dict[str, Any] | None,
) -> tuple[str, ...]:
    failures: list[str] = []
    if outcome != case.expected_outcome:
        failures.append("outcome_mismatch")
    if case.expected_reason != reason:
        failures.append("termination_reason_mismatch")
    if tools != case.expected_tools:
        failures.append("tool_sequence_mismatch")
    if case.expected_hard_eligible_ids and eligible_ids != case.expected_hard_eligible_ids:
        failures.append("hard_eligible_ids_mismatch")
    excluded = set(case.expected_excluded_ids)
    if excluded.intersection(eligible_ids) or excluded.intersection(recommended_ids):
        failures.append("excluded_merchant_present")
    if set(case.forbidden_tools).intersection(tools):
        failures.append("forbidden_tool_executed")
    if unresolved_items != case.expected_unresolved_items:
        failures.append("unresolved_items_mismatch")
    if outcome == "proposal" and citations_valid is not True:
        failures.append("citation_not_grounded")
    if case.expected_rule_fields:
        observed_fields = set()
        if rule_result is not None and isinstance(rule_result.get("rules"), dict):
            observed_fields = set(cast(dict[str, Any], rule_result["rules"]))
        if observed_fields != set(case.expected_rule_fields):
            failures.append("rule_fields_mismatch")
    return tuple(failures)


def _metrics(
    dataset: GoldenDataset,
    results: tuple[ScenarioACaseResult, ...],
) -> ScenarioAMetrics:
    count = len(results)
    critical = [result for result in results if result.critical]
    expected_by_id = {case.case_id: case for case in dataset.cases}
    proposals = [
        result
        for result in results
        if expected_by_id[result.case_id].expected_outcome == "proposal"
    ]
    return ScenarioAMetrics(
        case_pass_rate=sum(result.passed for result in results) / count,
        critical_pass_rate=sum(result.passed for result in critical) / len(critical),
        outcome_accuracy=sum(
            result.observed_outcome == expected_by_id[result.case_id].expected_outcome
            for result in results
        )
        / count,
        tool_sequence_accuracy=sum(
            result.executed_tools == expected_by_id[result.case_id].expected_tools
            for result in results
        )
        / count,
        grounded_proposal_rate=sum(result.citations_valid is True for result in proposals)
        / len(proposals),
    )


def create_scenario_a_baseline(
    report: ScenarioAReport,
    *,
    created_at: datetime,
) -> ScenarioABaseline:
    if not all(case.passed for case in report.cases):
        raise GoldenGateError("cannot create a baseline from failing Golden cases")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise GoldenGateError("baseline creation time must include a timezone")
    return ScenarioABaseline(
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        tool_schema_versions=report.tool_schema_versions,
        created_at=created_at,
        cases=report.cases,
        metrics=report.metrics,
    )


def load_scenario_a_baseline(path: Path) -> ScenarioABaseline:
    try:
        return ScenarioABaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenGateError("Scenario A baseline is unavailable or invalid") from exc


def load_scenario_a_gates(path: Path) -> ScenarioAGates:
    try:
        return ScenarioAGates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise GoldenGateError("Scenario A gates are unavailable or invalid") from exc


def assert_scenario_a_gates(
    report: ScenarioAReport,
    *,
    gates: ScenarioAGates,
    baseline: ScenarioABaseline | None = None,
) -> None:
    if report.suite != gates.suite or report.dataset_version != gates.dataset_version:
        raise GoldenGateError("Scenario A report and gates identify different suites")
    metrics = cast(dict[str, float], report.metrics.model_dump())
    for name, required in gates.required_metrics.items():
        if metrics[name] < required:
            raise GoldenGateError(f"Scenario A required metric failed: {name}")
    if not all(case.passed for case in report.cases):
        failed = ",".join(case.case_id for case in report.cases if not case.passed)
        raise GoldenGateError(f"Scenario A critical cases failed: {failed}")
    if baseline is None:
        return
    if (
        report.dataset_version != baseline.dataset_version
        or report.dataset_sha256 != baseline.dataset_sha256
        or report.runner_version != baseline.runner_version
        or report.tool_schema_versions != baseline.tool_schema_versions
    ):
        raise GoldenGateError("Scenario A report does not match the frozen baseline identity")
    if report.cases != baseline.cases:
        raise GoldenGateError("Scenario A per-case behavior regressed from baseline")
    baseline_metrics = cast(dict[str, float], baseline.metrics.model_dump())
    for name, prior in baseline_metrics.items():
        if metrics[name] < prior - gates.allowed_regression:
            raise GoldenGateError(f"Scenario A metric regressed from baseline: {name}")


def write_value_model(path: Path, value: ValueModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")
