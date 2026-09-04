"""Deterministic E2E-F coverage for the Scenario B research specialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from oria.agent import (
    ResearchLimits,
    ResearchRunContext,
    attribution_research_limits,
    build_attribution_graph,
    initial_attribution_state,
)
from oria.analytics.query import AnalyticsQueryStore
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ChatResult, TextBlock, ToolCall, Usage
from oria.data import initialize_data
from oria.eval.attribution_data import generate_attribution_fixture
from oria.permission.local import local_cli_executor, local_operator
from oria.tools.analytics import build_attribution_tool_registry

pytestmark = pytest.mark.integration

_PERIOD = {"start_date": "2026-08-30", "end_date": "2026-08-31"}


def _tool_data(messages: list[object], call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        value = message.model_dump(mode="json")
        if value.get("role") == "tool" and value.get("tool_call_id") == call_id:
            envelope = json.loads(value["content"])
            assert envelope["ok"] is True
            return envelope["data"]
    raise AssertionError(f"tool observation is missing: {call_id}")


def _tool_result(*calls: ToolCall) -> ChatResult:
    return ChatResult(
        content=(TextBlock(text="Inspecting bounded evidence."),),
        tool_calls=calls,
        usage=Usage(input_tokens=2, output_tokens=1),
    )


def _evidence(
    call_id: str, tool_name: str, path: str, value: object, hypothesis_id: str
) -> dict[str, object]:
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "data_path": path,
        "value": value,
        "supports": [hypothesis_id],
    }


class _AdaptiveAttributionProvider:
    def __init__(self, category: str, *, corrupt_evidence: bool = False) -> None:
        self.category = category
        self.corrupt_evidence = corrupt_evidence
        self.calls = 0
        self.selected_after_funnel: str | None = None
        self.visible_tools: list[tuple[str, ...] | None] = []

    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        del ctx
        names = None if tools is None else tuple(tool.name for tool in tools)
        self.visible_tools.append(names)
        assert getattr(getattr(options, "response_schema", None), "name", None) == (
            "attribution_conclusion_v1"
        )
        if self.calls == 0:
            result = _tool_result(
                ToolCall(
                    id="call-funnel",
                    name="query_funnel",
                    args={
                        "period": _PERIOD,
                        "dimensions": ["event_date"],
                        "region": "east",
                        "category": self.category,
                    },
                )
            )
        elif self.calls == 1:
            funnel = _tool_data(messages, "call-funnel")
            rates = [row["metrics"]["redemption_rate"] for row in funnel["rows"]]
            if rates[-1] < rates[0] - 0.2:
                self.selected_after_funnel = "query_activity"
                result = _tool_result(
                    ToolCall(
                        id="call-activity",
                        name="query_activity",
                        args={
                            "period": {
                                "start_date": "2026-08-29",
                                "end_date": "2026-09-01",
                            },
                            "category": self.category,
                        },
                    )
                )
            else:
                self.selected_after_funnel = "query_market_overview"
                result = _tool_result(self._market_call())
        elif self.selected_after_funnel == "query_activity" and self.calls == 2:
            result = _tool_result(self._market_call())
        elif self.selected_after_funnel == "query_activity":
            result = self._attributed(messages)
        else:
            result = ChatResult(
                content=(),
                tool_calls=(),
                structured_output={
                    "schema_version": 1,
                    "outcome": "insufficient",
                    "conclusion": None,
                    "hypotheses": [],
                    "evidence": [],
                    "confidence": 0.2,
                    "confidence_explanation": "Observed data does not isolate a cause.",
                    "abstained": True,
                    "requested_data": ["Additional dimension-level activity evidence."],
                },
                usage=Usage(input_tokens=2, output_tokens=2),
            )
        self.calls += 1
        return result

    def _market_call(self) -> ToolCall:
        return ToolCall(
            id="call-market",
            name="query_market_overview",
            args={
                "period": _PERIOD,
                "comparison": "previous_period",
                "dimensions": ["region", "category"],
                "region": "east",
                "category": self.category,
            },
        )

    def _attributed(self, messages: list[object]) -> ChatResult:
        funnel = _tool_data(messages, "call-funnel")
        activity = _tool_data(messages, "call-activity")
        market = _tool_data(messages, "call-market")
        observed_redemptions = funnel["rows"][-1]["metrics"]["redemptions"]
        cited_redemptions = (
            observed_redemptions + 1 if self.corrupt_evidence else observed_redemptions
        )
        output = {
            "schema_version": 1,
            "outcome": "attributed",
            "conclusion": "The local change aligns with the observed activity boundary.",
            "hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "statement": "A local activity boundary explains the conversion change.",
                    "uncertainty": "The fixture covers a bounded observation window.",
                }
            ],
            "evidence": [
                _evidence(
                    "call-funnel",
                    "query_funnel",
                    "/rows/1/metrics/redemptions",
                    cited_redemptions,
                    "h1",
                ),
                _evidence(
                    "call-activity",
                    "query_activity",
                    "/activities/0/ends_on",
                    activity["activities"][0]["ends_on"],
                    "h1",
                ),
                _evidence(
                    "call-market",
                    "query_market_overview",
                    "/segments/0/redemption_rate_change",
                    market["segments"][0]["redemption_rate_change"],
                    "h1",
                ),
            ],
            "confidence": 0.82,
            "confidence_explanation": "Three independent observations align; not a gate.",
            "abstained": False,
            "requested_data": [],
        }
        return ChatResult(
            content=(),
            tool_calls=(),
            structured_output=output,
            usage=Usage(input_tokens=3, output_tokens=4),
        )


class _ConflictingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        del ctx, tools, options
        if self.calls == 0:
            result = _tool_result(
                ToolCall(
                    id="conflict-funnel",
                    name="query_funnel",
                    args={
                        "period": _PERIOD,
                        "dimensions": ["event_date"],
                        "region": "east",
                        "category": "full_service",
                    },
                ),
                ToolCall(
                    id="conflict-market",
                    name="query_market_overview",
                    args={
                        "period": _PERIOD,
                        "comparison": "previous_period",
                        "dimensions": ["region", "category"],
                        "region": "east",
                        "category": "full_service",
                    },
                ),
            )
        else:
            funnel = _tool_data(messages, "conflict-funnel")
            market = _tool_data(messages, "conflict-market")
            result = ChatResult(
                content=(),
                tool_calls=(),
                structured_output={
                    "schema_version": 1,
                    "outcome": "conflicting",
                    "conclusion": None,
                    "hypotheses": [
                        {
                            "hypothesis_id": "local",
                            "statement": "The local funnel indicates deterioration.",
                            "uncertainty": "The market comparator differs.",
                        },
                        {
                            "hypothesis_id": "market",
                            "statement": "The broader market is comparatively stable.",
                            "uncertainty": "It may not explain the local segment.",
                        },
                    ],
                    "evidence": [
                        _evidence(
                            "conflict-funnel",
                            "query_funnel",
                            "/rows/1/metrics/redemption_rate",
                            funnel["rows"][1]["metrics"]["redemption_rate"],
                            "local",
                        ),
                        _evidence(
                            "conflict-market",
                            "query_market_overview",
                            "/segments/0/redemption_rate_change",
                            market["segments"][0]["redemption_rate_change"],
                            "market",
                        ),
                    ],
                    "confidence": 0.45,
                    "confidence_explanation": "The observations support different hypotheses.",
                    "abstained": False,
                    "requested_data": [],
                },
                usage=Usage(input_tokens=2, output_tokens=3),
            )
        self.calls += 1
        return result


class _RepeatingProvider:
    def __init__(self, *, parallel_calls: bool = False) -> None:
        self.calls = 0
        self.parallel_calls = parallel_calls

    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        del messages, ctx, tools, options
        calls = tuple(
            ToolCall(
                id=f"repeat-{self.calls}-{index}",
                name="query_funnel",
                args={
                    "period": _PERIOD,
                    "dimensions": ["event_date"],
                    "region": "east",
                    "category": "full_service",
                },
            )
            for index in range(2 if self.parallel_calls else 1)
        )
        self.calls += 1
        return _tool_result(*calls)


async def _run(
    tmp_path: Path,
    provider: object,
    *,
    limits: ResearchLimits | None = None,
) -> dict[str, Any]:
    query_database = tmp_path / "scenario-b" / "analytics.db"
    generate_attribution_fixture(query_database, tmp_path / "evaluation-only" / "labels.db")
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        registry = build_attribution_tool_registry(
            AnalyticsQueryStore(query_database), runtime.retriever
        )
        object.__setattr__(runtime, "llm", provider)
        object.__setattr__(runtime, "tools", registry)
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="v04-agent-session",
            thread_id="v04-agent-thread",
            run_id="v04-agent-run",
        )
        graph = build_attribution_graph(checkpointer=InMemorySaver())
        return await graph.ainvoke(
            initial_attribution_state(
                question="Explain the observed conversion change.",
                analysis_period="2026-08-30/2026-08-31",
            ),
            config={"configurable": {"thread_id": "v04-attribution-fixture"}},
            context=ResearchRunContext(ctx=ctx, limits=limits or attribution_research_limits()),
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "expected_next_tool", "expected_outcome"),
    [
        ("full_service", "query_activity", "attributed"),
        ("quick_service", "query_market_overview", "insufficient"),
    ],
)
async def test_intermediate_funnel_result_selects_a_non_fixed_path(
    tmp_path: Path,
    category: str,
    expected_next_tool: str,
    expected_outcome: str,
) -> None:
    provider = _AdaptiveAttributionProvider(category)

    result = await _run(tmp_path, provider)

    assert result["termination"] is None
    assert result["conclusion"]["outcome"] == expected_outcome
    assert provider.selected_after_funnel == expected_next_tool
    assert provider.visible_tools[0] == (
        "query_funnel",
        "drill_down",
        "query_activity",
        "query_market_overview",
        "search_history_experience",
    )
    if expected_outcome == "insufficient":
        assert result["conclusion"]["abstained"] is True
        assert result["final_result"] == result["conclusion"]
    else:
        assert len(result["conclusion"]["evidence"]) == 3


@pytest.mark.asyncio
async def test_conflicting_evidence_keeps_multiple_hypotheses_without_conclusion(
    tmp_path: Path,
) -> None:
    result = await _run(tmp_path, _ConflictingProvider())

    assert result["termination"] is None
    assert result["conclusion"]["outcome"] == "conflicting"
    assert result["conclusion"]["conclusion"] is None
    assert len(result["conclusion"]["hypotheses"]) == 2


@pytest.mark.asyncio
async def test_forged_evidence_fails_without_optimizer_repair(tmp_path: Path) -> None:
    provider = _AdaptiveAttributionProvider("full_service", corrupt_evidence=True)

    result = await _run(tmp_path, provider)

    assert result["conclusion"] is None
    assert result["termination"]["reason"] == "evidence_validation_failed"
    assert result["validation_repairs"] == 0
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_attribution_uses_shared_no_progress_termination(tmp_path: Path) -> None:
    provider = _RepeatingProvider()

    result = await _run(tmp_path, provider)

    assert result["termination"]["reason"] == "no_progress"
    assert result["no_progress_streak"] == 2
    assert result["tool_calls_total"] == 3
    assert len(result["seen_evidence_fingerprints"]) == 1
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_attribution_rejects_an_over_budget_batch_before_execution(
    tmp_path: Path,
) -> None:
    provider = _RepeatingProvider(parallel_calls=True)

    result = await _run(
        tmp_path,
        provider,
        limits=ResearchLimits(max_tool_calls=1),
    )

    assert result["termination"]["reason"] == "max_tool_calls"
    assert result["tool_calls_total"] == 0
    assert result["tool_results"] == {}
    assert provider.calls == 1
