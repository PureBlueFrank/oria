"""Contracts for deterministic supervisor routing and bounded handoffs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from oria.agent import (
    SubagentResult,
    SupervisorHandoff,
    SupervisorRouter,
    SupervisorRunContext,
    build_supervisor_graph,
    initial_supervisor_state,
)
from oria.agent.models import AgentTermination
from oria.core.context import Context

pytestmark = pytest.mark.contract


def _run_context() -> SupervisorRunContext:
    ctx = cast(Context, SimpleNamespace(run_id="supervisor-contract-run"))
    return SupervisorRunContext(ctx=ctx)


def _termination(reason: str = "fixture_failed") -> AgentTermination:
    return AgentTermination(
        status="failed",
        reason=reason,
        limits={},
        observed_usage={},
    )


def test_handoff_and_result_are_strict_json_roundtrip_values() -> None:
    handoff = SupervisorHandoff(
        subagent_name="campaign_research",
        task="生成华东餐饮招商建议",
        source_run_id="run-1",
    )
    result = SubagentResult(
        subagent_name="campaign_research",
        status="completed",
        result={"abstained": False},
        allowlisted_tools=("search_campaign_rules", "query_merchants"),
        visible_tools=("search_campaign_rules",),
    )

    assert SupervisorHandoff.model_validate_json(handoff.model_dump_json()) == handoff
    assert SubagentResult.model_validate_json(result.model_dump_json()) == result
    assert json.loads(handoff.model_dump_json())["schema_version"] == 1
    with pytest.raises(ValidationError):
        SupervisorHandoff.model_validate({**handoff.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError, match="subset"):
        SubagentResult(
            subagent_name="campaign_research",
            status="completed",
            result={},
            allowlisted_tools=("search_campaign_rules",),
            visible_tools=("query_merchants",),
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("生成华东餐饮招商活动建议", "campaign_research"),
        ("查找适合活动的商家", "campaign_research"),
        ("华东餐饮招商转化率为什么下降", "attribution_research"),
        ("Investigate the root cause of this funnel anomaly", "attribution_research"),
    ],
)
def test_router_is_deterministic_and_attribution_wins_overlap(
    query: str, expected: str
) -> None:
    router = SupervisorRouter()

    first = router.route(query)
    second = router.route(query)

    assert first == second
    assert first is not None
    assert first.subagent_name == expected


def test_router_rejects_unclassified_requests() -> None:
    assert SupervisorRouter().route("你好") is None


@pytest.mark.asyncio
async def test_max_handoff_limit_stops_before_invoking_another_subagent() -> None:
    called = False

    async def invoke(*args: object) -> SubagentResult:
        nonlocal called
        called = True
        raise AssertionError(args)

    state = initial_supervisor_state(
        user_request="生成招商活动建议",
        effective_at="2026-09-10T00:00:00+08:00",
        max_handoffs=1,
    )
    state["handoffs"] = [
        SupervisorHandoff(
            subagent_name="campaign_research",
            task="prior task",
            source_run_id="prior-run",
        ).model_dump(mode="json")
    ]

    output = await build_supervisor_graph(invoke_subagent=invoke).ainvoke(
        state,
        context=_run_context(),
    )

    assert called is False
    assert output["termination"]["reason"] == "max_handoffs_exceeded"
    assert output["termination"]["handoffs"] == 1


@pytest.mark.asyncio
async def test_failed_subagent_is_recovered_and_reported_without_retry() -> None:
    calls: list[SupervisorHandoff] = []

    async def invoke(
        handoff: SupervisorHandoff,
        state: object,
        context: object,
    ) -> SubagentResult:
        del state, context
        calls.append(handoff)
        return SubagentResult(
            subagent_name=handoff.subagent_name,
            status="failed",
            termination=_termination(),
            allowlisted_tools=("search_campaign_rules", "query_merchants"),
            visible_tools=(),
        )

    output: dict[str, Any] = await build_supervisor_graph(invoke_subagent=invoke).ainvoke(
        initial_supervisor_state(
            user_request="生成招商活动建议",
            effective_at="2026-09-10T00:00:00+08:00",
        ),
        context=_run_context(),
    )

    assert len(calls) == 1
    assert output["final_result"] is None
    assert output["termination"]["reason"] == "subagent_failed:fixture_failed"
    assert output["subagent_results"][0]["status"] == "failed"
    assert output["events"][-1]["type"] == "subagent_recovered"
