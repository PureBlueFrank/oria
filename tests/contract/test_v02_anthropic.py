"""V0.2 Anthropic Messages request, response, and streaming contracts."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oria.config.models import ResolvedLLMProfile
from oria.core.types import (
    ChatOptions,
    Done,
    Message,
    ProviderError,
    ReasoningDelta,
    ResponseSchema,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolSpec,
    UsageDelta,
)
from oria.providers.anthropic import AnthropicProvider
from oria.providers.errors import StructuredOutputError

pytestmark = pytest.mark.contract


def _profile(*, mode: str = "native_json_schema") -> ResolvedLLMProfile:
    return ResolvedLLMProfile.model_validate(
        {
            "profile_id": "anthropic",
            "provider": "anthropic",
            "api_dialect": "anthropic_messages",
            "model": "claude-fixture-model",
            "api_key": "anthropic-test-key",
            "base_url": "https://api.anthropic.com",
            "structured_output_mode": mode,
        }
    )


def _schema() -> ResponseSchema:
    return ResponseSchema(
        name="answer",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )


def _tool() -> ToolSpec:
    return ToolSpec(
        name="lookup",
        schema_version=1,
        description="Lookup a record.",
        json_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )


def _message(*blocks: dict[str, object]) -> dict[str, object]:
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "claude-fixture-model",
        "content": list(blocks),
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 13,
            "output_tokens": 8,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
        },
    }


@pytest.mark.asyncio
async def test_anthropic_maps_system_content_blocks_tools_and_usage_in_order() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = _message(
            {"type": "text", "text": "visible"},
            {"type": "thinking", "thinking": "private chain", "signature": "secret"},
            {"type": "tool_use", "id": "call-2", "name": "lookup", "input": {"id": "2"}},
        )
        body["api_key"] = "echoed-secret"
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        result = await AnthropicProvider(_profile(), client).chat(
            [
                Message(role="system", content="first"),
                Message(role="system", content="second"),
                Message(role="user", content="question"),
                Message(
                    role="assistant",
                    content=(
                        TextBlock(text="checking"),
                        ToolCallBlock(id="call-1", name="lookup", args={"id": "1"}),
                    ),
                ),
                Message(role="tool", tool_call_id="call-1", content='{"name":"one"}'),
            ],
            None,  # type: ignore[arg-type]
            tools=[_tool()],
            options=ChatOptions(max_output_tokens=256),
        )

    request = captured[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "anthropic-test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(request.content)
    assert payload["system"] == "first\n\nsecond"
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "question"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lookup",
                    "input": {"id": "1"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": '{"name":"one"}',
                }
            ],
        },
    ]
    assert payload["tools"] == [
        {
            "name": "lookup",
            "description": "Lookup a record.",
            "input_schema": _tool().model_dump(mode="json")["json_schema"],
            "strict": True,
        }
    ]
    assert payload["max_tokens"] == 256
    assert [type(block) for block in result.content] == [TextBlock, ToolCallBlock]
    assert result.text == "visible"
    assert result.tool_calls[0].args == {"id": "2"}
    assert result.usage.cache_read_tokens == 4
    assert result.usage.cache_write_tokens == 2
    assert result.usage.reasoning_tokens is None
    diagnostic = str(result.internal_raw_response())
    assert "private chain" not in diagnostic
    assert "echoed-secret" not in diagnostic


@pytest.mark.asyncio
async def test_anthropic_native_output_format_can_coexist_with_strict_business_tools() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message({"type": "text", "text": '{"answer":"ok"}'}))

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        result = await AnthropicProvider(_profile(), client).chat(
            [Message(role="user", content="answer")],
            None,  # type: ignore[arg-type]
            tools=[_tool()],
            options=ChatOptions(response_schema=_schema()),
        )

    assert result.structured_output == {"answer": "ok"}
    assert captured[0]["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": _schema().model_dump(mode="json")["json_schema"],
        }
    }
    assert [tool["name"] for tool in captured[0]["tools"]] == ["lookup"]
    assert captured[0]["tools"][0]["strict"] is True


@pytest.mark.asyncio
async def test_anthropic_synthetic_tool_is_intercepted_and_cannot_mix() -> None:
    captured: list[dict[str, Any]] = []
    responses = iter(
        [
            _message(
                {
                    "type": "tool_use",
                    "id": "submit-1",
                    "name": "__oria_submit_response__",
                    "input": {"answer": "ok"},
                }
            ),
            _message(
                {
                    "type": "tool_use",
                    "id": "submit-2",
                    "name": "__oria_submit_response__",
                    "input": {"answer": "ok"},
                },
                {"type": "tool_use", "id": "business-1", "name": "lookup", "input": {"id": "1"}},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = AnthropicProvider(_profile(mode="synthetic_tool"), client)
        options = ChatOptions(response_schema=_schema())
        result = await provider.chat(
            [Message(role="user", content="answer")],
            None,  # type: ignore[arg-type]
            options=options,
        )
        with pytest.raises(StructuredOutputError, match="mixed"):
            await provider.chat(
                [Message(role="user", content="mixed")],
                None,  # type: ignore[arg-type]
                tools=[_tool()],
                options=options,
            )

    assert result.structured_output == {"answer": "ok"}
    assert result.tool_calls == ()
    assert "output_config" not in captured[0]
    assert captured[0]["tools"][0]["name"] == "__oria_submit_response__"


@pytest.mark.asyncio
async def test_anthropic_stream_maps_text_tool_reasoning_usage_and_done() -> None:
    events_payload = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-stream",
                "model": "claude-fixture-model",
                "usage": {
                    "input_tokens": 17,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 5,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "private"},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"id":'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '"1"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 9, "reasoning_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    body = "\n\n".join(f"data: {json.dumps(event)}" for event in events_payload)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in AnthropicProvider(_profile(), client).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
                tools=[_tool()],
            )
        ]

    assert [type(event) for event in events] == [
        TextDelta,
        ReasoningDelta,
        ToolCallDelta,
        ToolCallDelta,
        UsageDelta,
        Done,
    ]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[1].internal_text() == "private"
    assert events[2].tool_call_id == "call-1"
    assert events[-2].usage.input_tokens == 17
    assert events[-2].usage.output_tokens == 9
    assert events[-2].usage.reasoning_tokens == 3
    assert events[-1].finish_reason == "tool_use"


@pytest.mark.asyncio
async def test_anthropic_synthetic_stream_buffers_reserved_submission_until_valid() -> None:
    events_payload = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-stream",
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "submit-1",
                "name": "__oria_submit_response__",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"answer":"ok"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]
    body = "\n\n".join(f"data: {json.dumps(event)}" for event in events_payload)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in AnthropicProvider(
                _profile(mode="synthetic_tool"), client
            ).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )
        ]

    assert [type(event) for event in events] == [TextDelta, UsageDelta, Done]
    assert events[0].text == '{"answer":"ok"}'


@pytest.mark.asyncio
async def test_anthropic_native_stream_rejects_schema_mismatch() -> None:
    events_payload = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-stream",
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": '{"answer":1}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]
    body = "\n\n".join(f"data: {json.dumps(event)}" for event in events_payload)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in AnthropicProvider(_profile(), client).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )
        ]

    assert [type(event) for event in events] == [UsageDelta, ProviderError]
    assert events[-1].code == "structured_output_error"
