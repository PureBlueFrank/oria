"""Fixture E2E coverage for the permanent V0.1 research graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from oria.agent import (
    ResearchRunContext,
    build_research_graph,
    initial_research_state,
)
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ChatResult, TextBlock, ToolCall, Usage
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document

pytestmark = pytest.mark.integration


class _ScenarioAProvider:
    def __init__(self, *, forged_merchant_id: str | None = None, query_limit: int = 10) -> None:
        self.calls = 0
        self.forged_merchant_id = forged_merchant_id
        self.query_limit = query_limit
        self.visible_query_maximum: int | None = None

    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        del ctx, options
        if tools is not None:
            query_spec = next(tool for tool in tools if tool.name == "query_merchants")
            self.visible_query_maximum = query_spec.json_schema["properties"]["limit"]["maximum"]
        if self.calls == 0:
            assert tools is not None and len(tools) == 2
            result = ChatResult(
                content=(TextBlock(text="正在读取规则"),),
                tool_calls=(
                    ToolCall(
                        id="call-rules",
                        name="search_campaign_rules",
                        args={
                            "intent": "merchant_recruitment",
                            "effective_at": "2026-07-15T00:00:00+08:00",
                        },
                    ),
                ),
                usage=Usage(input_tokens=10, output_tokens=4),
                finish_reason="arbitrary-provider-value",
            )
        elif self.calls == 1:
            search = _tool_data(messages, "call-rules")
            result = ChatResult(
                content=(),
                tool_calls=(
                    ToolCall(
                        id="call-merchants",
                        name="query_merchants",
                        args={
                            "rule_snapshot_id": search["rule_snapshot_id"],
                            "limit": self.query_limit,
                        },
                    ),
                ),
                usage=Usage(input_tokens=12, output_tokens=4),
                finish_reason="stop",
            )
        else:
            assert tools is not None and len(tools) == 2
            search = _tool_data(messages, "call-rules")
            merchants = _tool_data(messages, "call-merchants")
            result = ChatResult(
                content=(),
                tool_calls=(),
                structured_output=_proposal(
                    search,
                    merchants,
                    forged_merchant_id=self.forged_merchant_id,
                ),
                usage=Usage(input_tokens=15, output_tokens=20, cost=0.01),
                finish_reason="tool_calls",
            )
        self.calls += 1
        return result


def _tool_data(messages: list[object], call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        value = message.model_dump(mode="json")
        if value.get("role") == "tool" and value.get("tool_call_id") == call_id:
            envelope = json.loads(value["content"])
            assert envelope["ok"] is True
            return envelope["data"]
    raise AssertionError(f"tool observation is missing: {call_id}")


def _proposal(
    search: dict[str, Any],
    merchants: dict[str, Any],
    *,
    forged_merchant_id: str | None,
) -> dict[str, Any]:
    del search
    recommendations = [
        {
            "merchant_id": candidate["merchant_id"],
            "rank": index,
            "reason": "满足已验证规则快照中的全部硬资格",
        }
        for index, candidate in enumerate(merchants["candidates"], start=1)
    ]
    if forged_merchant_id is not None:
        recommendations[-1] = {
            "merchant_id": forged_merchant_id,
            "rank": len(recommendations),
            "reason": "模型伪造的候选",
        }
    return {
        "schema_version": 1,
        "recommended_merchants": recommendations,
        "unresolved_items": [],
        "abstained": False,
    }


async def _run(
    tmp_path: Path, provider: _ScenarioAProvider, *, max_candidates: int = 10
) -> dict[str, Any]:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    services = await build_runtime(config)
    try:
        object.__setattr__(services, "llm", provider)
        ctx = services.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="t07-session",
            thread_id="t07-thread",
            run_id="t07-run",
        )
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        graph = build_research_graph(checkpointer=InMemorySaver())
        result = await graph.ainvoke(
            initial_research_state(
                user_request="生成华东餐饮招商活动建议",
                effective_at="2026-07-15T00:00:00+08:00",
                max_candidates=max_candidates,
            ),
            config={"configurable": {"thread_id": "fixture-e2e"}},
            context=ResearchRunContext(ctx=ctx),
        )
        return result
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_fixture_graph_produces_cited_ten_merchant_proposal(tmp_path: Path) -> None:
    result = await _run(tmp_path, _ScenarioAProvider())

    assert result["termination"] is None
    assert result["proposal"] is not None
    assert len(result["proposal"]["recommended_merchants"]) == 10
    assert result["proposal"]["rules"] == result["rule_result"]["rules"]
    assert result["proposal"]["field_evidence"] == result["rule_result"]["field_evidence"]
    assert result["model_turns"] == 3
    assert result["tool_calls_total"] == 2
    assert result["validation_repairs"] == 0
    assert result["input_tokens"] == 37
    assert result["output_tokens"] == 28
    tool_messages = [message for message in result["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call-rules",
        "call-merchants",
    ]
    for message in tool_messages:
        envelope = json.loads(message["content"])
        assert message["content"] == json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert tuple(envelope) == tuple(sorted(envelope))


@pytest.mark.asyncio
@pytest.mark.security
async def test_hard_excluded_merchant_cannot_be_repaired_into_proposal(tmp_path: Path) -> None:
    result = await _run(tmp_path, _ScenarioAProvider(forged_merchant_id="demo-m004"))

    assert result["proposal"] is None
    assert result["termination"]["reason"] == "evidence_validation_failed"
    assert result["validation_repairs"] == 0
    assert result["model_turns"] == 3


@pytest.mark.asyncio
async def test_query_limit_cannot_exceed_the_requested_candidate_limit(tmp_path: Path) -> None:
    provider = _ScenarioAProvider(query_limit=2)
    result = await _run(
        tmp_path,
        provider,
        max_candidates=1,
    )

    assert result["proposal"] is None
    assert result["termination"]["reason"] == "policy_or_contract_violation"
    assert result["events"][-1]["error_code"] == "invalid_arguments"
    assert result["tool_calls_total"] == 1
    assert provider.visible_query_maximum == 1
