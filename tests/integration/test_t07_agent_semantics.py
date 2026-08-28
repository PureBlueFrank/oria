"""Failure, repair, budget, ordering, and no-progress semantics for T07."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oria.agent import (
    ResearchLimits,
    ResearchRunContext,
    build_research_graph,
    initial_research_state,
)
from oria.agent.observations import build_observation, canonical_json
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ChatResult, PolicyDecision, TextBlock, ToolCall, ToolResult, Usage
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.errors import StructuredOutputError
from oria.rag.demo import demo_rule_document

pytestmark = pytest.mark.integration


class _SequenceProvider:
    def __init__(self, results: list[ChatResult | Exception]) -> None:
        self.results = results
        self.calls = 0
        self.visible_tool_names: list[tuple[str, ...] | None] = []

    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        del messages, ctx, options
        self.visible_tool_names.append(
            None if tools is None else tuple(tool.name for tool in tools)
        )
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class _SlowSequenceProvider(_SequenceProvider):
    async def chat(
        self,
        messages: list[object],
        ctx: object,
        tools: list[object] | None = None,
        options: object | None = None,
    ) -> ChatResult:
        await asyncio.sleep(0.02)
        return await super().chat(messages, ctx, tools, options)


class _DenyPolicy:
    async def authorize(self, request: object, ctx: object) -> PolicyDecision:
        del request, ctx
        return PolicyDecision(
            allow=False,
            policy_version="deny-test-v1",
            reason="test denial",
        )


def _tool_result(*calls: ToolCall, usage: Usage | None = None) -> ChatResult:
    return ChatResult(
        content=(TextBlock(text="provisional"),),
        tool_calls=calls,
        usage=usage or Usage(input_tokens=1, output_tokens=1),
        finish_reason="ignored",
    )


def _abstain_result() -> ChatResult:
    return ChatResult(
        content=(),
        tool_calls=(),
        structured_output={
            "schema_version": 1,
            "recommended_merchants": [],
            "field_evidence": {},
            "unresolved_items": ["insufficient evidence"],
            "abstained": True,
        },
        usage=Usage(input_tokens=1, output_tokens=1),
    )


async def _runtime(tmp_path: Path, provider: _SequenceProvider):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    services = await build_runtime(config)
    object.__setattr__(services, "llm", provider)
    ctx = services.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="semantics-session",
        thread_id="semantics-thread",
        run_id="semantics-run",
    )
    await ctx.knowledge.ingest(demo_rule_document(), ctx)
    return services, ctx


async def _invoke(
    tmp_path: Path,
    provider: _SequenceProvider,
    *,
    limits: ResearchLimits | None = None,
    deadline_at: datetime | None = None,
    deny_tools: bool = False,
) -> dict[str, Any]:
    services, ctx = await _runtime(tmp_path, provider)
    try:
        if deny_tools:
            object.__setattr__(services, "policy", _DenyPolicy())
        return await build_research_graph().ainvoke(
            initial_research_state(
                user_request="生成招商建议",
                effective_at="2026-07-15T00:00:00+08:00",
            ),
            context=ResearchRunContext(
                ctx=ctx,
                limits=limits or ResearchLimits(),
                deadline_at=deadline_at,
            ),
        )
    finally:
        await services.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_call", "expected_code"),
    [
        (ToolCall(id="invalid", name="persist_campaign", args={}), "unknown_tool"),
        (
            ToolCall(
                id="invalid",
                name="search_campaign_rules",
                args={
                    "intent": "merchant_recruitment",
                    "effective_at": "2026-07-15T00:00:00",
                },
            ),
            "invalid_arguments",
        ),
    ],
)
async def test_invalid_call_rejects_entire_parallel_batch_before_execution(
    tmp_path: Path,
    invalid_call: ToolCall,
    expected_code: str,
) -> None:
    provider = _SequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="valid",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                ),
                invalid_call,
            )
        ]
    )
    result = await _invoke(tmp_path, provider)

    assert result["termination"]["reason"] == "policy_or_contract_violation"
    assert result["tool_calls_total"] == 0
    assert not [message for message in result["messages"] if message["role"] == "tool"]
    assert result["events"][-1]["error_code"] == expected_code


@pytest.mark.asyncio
async def test_permission_denial_rejects_batch_before_execution(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="denied",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                )
            )
        ]
    )
    result = await _invoke(tmp_path, provider, deny_tools=True)

    assert result["termination"]["reason"] == "policy_or_contract_violation"
    assert result["tool_calls_total"] == 0
    assert result["events"][-1]["error_code"] == "permission_denied"


@pytest.mark.asyncio
async def test_plain_output_gets_one_finalization_only_repair(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            ChatResult(
                content=(TextBlock(text="plain text cannot be final"),),
                tool_calls=(),
                usage=Usage(input_tokens=1, output_tokens=1),
            ),
            _abstain_result(),
        ]
    )
    result = await _invoke(tmp_path, provider)

    assert result["termination"] is None, result
    assert result["proposal"]["abstained"] is True
    assert result["validation_repairs"] == 1
    assert provider.visible_tool_names == [
        ("search_campaign_rules", "query_merchants"),
        None,
    ]
    assert "plain text cannot be final" not in json.dumps(result["proposal"])


@pytest.mark.asyncio
async def test_structured_provider_error_counts_turn_and_repairs_once(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            StructuredOutputError("invalid structured output", retryable=False),
            _abstain_result(),
        ]
    )
    result = await _invoke(tmp_path, provider)

    assert result["termination"] is None, result
    assert result["proposal"]["abstained"] is True
    assert result["model_turns"] == 2
    assert result["validation_repairs"] == 1
    assert provider.visible_tool_names == [
        ("search_campaign_rules", "query_merchants"),
        None,
    ]


@pytest.mark.asyncio
async def test_provider_usage_over_limit_prevents_suggested_tool_execution(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="over-budget",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                ),
                usage=Usage(input_tokens=1, output_tokens=11),
            )
        ]
    )
    result = await _invoke(tmp_path, provider, limits=ResearchLimits(max_output_tokens=10))

    assert result["termination"]["reason"] == "max_output_tokens"
    assert result["tool_calls_total"] == 0
    assert not [message for message in result["messages"] if message["role"] == "tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "limits", "reason"),
    [
        (
            Usage(input_tokens=11, output_tokens=1),
            ResearchLimits(max_input_tokens=10),
            "max_input_tokens",
        ),
        (
            Usage(input_tokens=6, output_tokens=5),
            ResearchLimits(max_total_tokens=10),
            "max_total_tokens",
        ),
        (
            Usage(input_tokens=1, output_tokens=1, cost=0.11),
            ResearchLimits(max_cost=0.1),
            "max_cost",
        ),
    ],
)
async def test_other_provider_usage_limits_also_block_tools(
    tmp_path: Path,
    usage: Usage,
    limits: ResearchLimits,
    reason: str,
) -> None:
    provider = _SequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="over-budget",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                ),
                usage=usage,
            )
        ]
    )
    result = await _invoke(tmp_path, provider, limits=limits)

    assert result["termination"]["reason"] == reason
    assert result["tool_calls_total"] == 0


@pytest.mark.asyncio
async def test_model_tool_and_deadline_limits_stop_without_extra_execution(tmp_path: Path) -> None:
    plain = ChatResult(
        content=(TextBlock(text="not structured"),),
        tool_calls=(),
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    model_provider = _SequenceProvider([plain])
    model_result = await _invoke(
        tmp_path / "model",
        model_provider,
        limits=ResearchLimits(max_model_turns=1),
    )
    assert model_result["termination"]["reason"] == "max_model_turns"
    assert model_provider.calls == 1

    two_calls = tuple(
        ToolCall(
            id=f"budget-{index}",
            name="search_campaign_rules",
            args={
                "intent": "merchant_recruitment",
                "effective_at": "2026-07-15T00:00:00+08:00",
            },
        )
        for index in range(2)
    )
    tool_result = await _invoke(
        tmp_path / "tools",
        _SequenceProvider([_tool_result(*two_calls)]),
        limits=ResearchLimits(max_tool_calls=1),
    )
    assert tool_result["termination"]["reason"] == "max_tool_calls"
    assert tool_result["tool_calls_total"] == 0

    deadline_provider = _SequenceProvider([])
    deadline_result = await _invoke(
        tmp_path / "deadline",
        deadline_provider,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert deadline_result["termination"]["reason"] == "deadline_exceeded"
    assert deadline_provider.calls == 0

    slow_provider = _SlowSequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="after-deadline",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                )
            )
        ]
    )
    slow_result = await _invoke(
        tmp_path / "deadline-during-model",
        slow_provider,
        deadline_at=datetime.now(UTC) + timedelta(milliseconds=5),
    )
    assert slow_result["termination"]["reason"] == "deadline_exceeded"
    assert slow_result["tool_calls_total"] == 0


@pytest.mark.asyncio
async def test_two_consecutive_duplicate_evidence_steps_terminate_no_progress(
    tmp_path: Path,
) -> None:
    results = [
        _tool_result(
            ToolCall(
                id=f"same-evidence-{index}",
                name="search_campaign_rules",
                args={
                    "intent": "merchant_recruitment",
                    "effective_at": "2026-07-15T00:00:00+08:00",
                },
            )
        )
        for index in range(3)
    ]
    provider = _SequenceProvider(results)
    result = await _invoke(tmp_path, provider)

    assert result["termination"]["reason"] == "no_progress"
    assert result["no_progress_streak"] == 2
    assert result["tool_calls_total"] == 3
    assert len(result["seen_evidence_fingerprints"]) == 1
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_parallel_observations_preserve_declaration_order(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            _tool_result(
                ToolCall(
                    id="declared-first",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                ),
                ToolCall(
                    id="declared-second",
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-16T00:00:00+08:00",
                    },
                ),
            ),
            _abstain_result(),
        ]
    )
    result = await _invoke(tmp_path, provider)

    tool_messages = [message for message in result["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "declared-first",
        "declared-second",
    ]
    assert result["proposal"]["abstained"] is True


@pytest.mark.asyncio
async def test_large_observation_spills_without_inline_fallback(tmp_path: Path) -> None:
    provider = _SequenceProvider([])
    services, ctx = await _runtime(tmp_path, provider)
    try:
        data = {"payload": "sensitive-but-authorized-" + "x" * 1024}
        result = ToolResult(
            ok=True,
            data=data,
            execution_id="large-result",
            trust_level="trusted_internal",
            provenance="oria://test/large/v1",
            data_classification="internal",
        )
        built = build_observation(
            ToolCall(id="large-call", name="search_campaign_rules", args={}),
            result,
            tool_schema_version=1,
            max_inline_bytes=256,
            ctx=ctx,
        )
        envelope = json.loads(built.canonical_json)
        assert envelope["object_ref"]["byte_size"] == len(canonical_json(data).encode("utf-8"))
        assert "sensitive-but-authorized" not in built.canonical_json
        assert envelope["data"]["truncated"] is True

        object.__setattr__(services, "objects", None)
        with pytest.raises(RuntimeError, match="object store"):
            build_observation(
                ToolCall(id="failed-spill", name="search_campaign_rules", args={}),
                result,
                tool_schema_version=1,
                max_inline_bytes=256,
                ctx=ctx,
            )
    finally:
        await services.aclose()
