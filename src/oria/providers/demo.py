"""Stateless deterministic MockLLM behavior for the offline Scenario A demo."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from oria.core.types import (
    ChatOptions,
    ChatResult,
    Done,
    JsonValue,
    Message,
    StreamEvent,
    TextBlock,
    ToolCall,
    ToolSpec,
    Usage,
)
from oria.providers.errors import StructuredOutputError
from oria.providers.mock import MockLLMProvider
from oria.providers.structured import validate_structured_value

if TYPE_CHECKING:
    from oria.core.context import Context

_RULE_CALL_ID = "demo-search-rules"
_MERCHANT_CALL_ID = "demo-query-merchants"


class DemoMockLLMProvider(MockLLMProvider):
    """Drive the real research graph from observations without retaining run state."""

    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        del ctx
        available = frozenset(tool.name for tool in tools or ())
        expected = frozenset({"search_campaign_rules", "query_merchants"})
        if tools is not None and available != expected:
            raise StructuredOutputError("demo tool contract is unavailable", retryable=False)

        rules = _tool_data(messages, _RULE_CALL_ID)
        if rules is None:
            return _tool_result(
                ToolCall(
                    id=_RULE_CALL_ID,
                    name="search_campaign_rules",
                    args={
                        "intent": "merchant_recruitment",
                        "effective_at": "2026-07-15T00:00:00+08:00",
                    },
                )
            )
        if rules.get("rules") is None:
            unresolved = rules.get("unresolved_items")
            if not isinstance(unresolved, list) or not all(
                isinstance(item, str) for item in unresolved
            ):
                raise StructuredOutputError("demo rule observation is malformed", retryable=False)
            return _structured_result(
                {
                    "schema_version": 1,
                    "unresolved_items": unresolved,
                    "abstained": True,
                },
                options,
            )

        merchants = _tool_data(messages, _MERCHANT_CALL_ID)
        if merchants is None:
            snapshot_id = rules.get("rule_snapshot_id")
            if not isinstance(snapshot_id, str):
                raise StructuredOutputError("demo rule snapshot is malformed", retryable=False)
            return _tool_result(
                ToolCall(
                    id=_MERCHANT_CALL_ID,
                    name="query_merchants",
                    args={"rule_snapshot_id": snapshot_id, "limit": 10},
                )
            )
        return _structured_result(_proposal(rules, merchants), options)

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
            provider="mock",
            model="mock-demo",
            request_id=result.request_id,
            finish_reason=result.finish_reason,
        )


def _tool_result(call: ToolCall) -> ChatResult:
    return ChatResult(
        content=(TextBlock(text="offline fixture tool request"),),
        tool_calls=(call,),
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="tool_calls",
        request_id=f"mock-{call.id}",
    )


def _structured_result(value: dict[str, JsonValue], options: ChatOptions | None) -> ChatResult:
    schema = (options or ChatOptions()).response_schema
    if schema is None:
        raise StructuredOutputError("demo requires a response schema", retryable=False)
    structured = validate_structured_value(value, schema)
    return ChatResult(
        content=(),
        tool_calls=(),
        structured_output=structured,
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="stop",
        request_id="mock-demo-proposal",
    )


def _tool_data(messages: list[Message], call_id: str) -> dict[str, JsonValue] | None:
    for message in reversed(messages):
        if message.role != "tool" or message.tool_call_id != call_id:
            continue
        if not isinstance(message.content, str):
            raise StructuredOutputError("demo tool observation is malformed", retryable=False)
        try:
            envelope: Any = json.loads(message.content)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(
                "demo tool observation is malformed", retryable=False
            ) from exc
        if not isinstance(envelope, dict):
            raise StructuredOutputError("demo tool observation is malformed", retryable=False)
        data = envelope.get("data")
        if envelope.get("ok") is not True or not isinstance(data, dict):
            raise StructuredOutputError("demo tool observation failed", retryable=False)
        return cast(dict[str, JsonValue], data)
    return None


def _proposal(
    search: dict[str, JsonValue], merchants: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    del search
    candidates = merchants.get("candidates")
    if not isinstance(candidates, list):
        raise StructuredOutputError("demo evidence is malformed", retryable=False)
    recommendations: list[JsonValue] = []
    for rank, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("merchant_id"), str):
            raise StructuredOutputError("demo merchant evidence is malformed", retryable=False)
        recommendations.append(
            {
                "merchant_id": candidate["merchant_id"],
                "rank": rank,
                "reason": "满足规则快照中的全部确定性硬资格且可进入后续运营审核。",
            }
        )
    return {
        "schema_version": 1,
        "recommended_merchants": recommendations,
        "unresolved_items": [],
        "abstained": False,
    }
