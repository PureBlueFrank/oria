"""V0.2 OpenAI-compatible Chat Completions dialect contracts."""

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
from oria.providers.errors import (
    AuthenticationError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)
from oria.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.contract


def _profile(
    provider: str = "openai", *, mode: str = "native_json_schema", key: str | None = "key"
) -> ResolvedLLMProfile:
    return ResolvedLLMProfile.model_validate(
        {
            "profile_id": provider,
            "provider": provider,
            "api_dialect": "chat_completions",
            "model": f"{provider}-fixture-model",
            "api_key": key,
            "base_url": "https://provider.invalid/v1",
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


def _usage() -> dict[str, object]:
    return {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def _completion(message: dict[str, object]) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "model": "fixture-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": _usage(),
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["kimi", "zhipu", "openai"])
async def test_chat_completions_maps_messages_tools_reasoning_and_usage(
    provider_name: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "visible",
                    "reasoning_content": "private reasoning",
                    "tool_calls": [
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":"2"}'},
                        }
                    ],
                }
            ),
        )

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        result = await OpenAICompatProvider(_profile(provider_name), client).chat(
            [
                Message(role="system", content="system"),
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
            options=ChatOptions(max_output_tokens=128),
        )

    assert captured[0].url.path == "/v1/chat/completions"
    payload = json.loads(captured[0].content)
    assert payload["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"name":"one"}'},
    ]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup a record.",
                "parameters": _tool().model_dump(mode="json")["json_schema"],
            },
        }
    ]
    assert payload["max_tokens"] == 128
    assert result.text == "visible"
    assert [call.name for call in result.tool_calls] == ["lookup"]
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.cache_read_tokens == 3
    assert result.usage.reasoning_tokens == 2
    assert "private reasoning" not in str(result.content)
    assert "private reasoning" not in str(result.internal_raw_response())


@pytest.mark.asyncio
async def test_chat_completions_native_schema_is_explicit_and_locally_validated() -> None:
    captured: list[dict[str, Any]] = []
    responses = iter(
        [
            _completion({"role": "assistant", "content": '{"answer":"ok"}'}),
            _completion({"role": "assistant", "content": '{"answer":1}'}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatProvider(_profile(), client)
        options = ChatOptions(response_schema=_schema())
        result = await provider.chat(
            [Message(role="user", content="answer")],
            None,
            options=options,  # type: ignore[arg-type]
        )
        with pytest.raises(StructuredOutputError):
            await provider.chat(
                [Message(role="user", content="invalid")],
                None,  # type: ignore[arg-type]
                options=options,
            )

    assert result.structured_output == {"answer": "ok"}
    assert captured[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "schema": _schema().model_dump(mode="json")["json_schema"],
            "strict": True,
        },
    }
    assert "text" not in captured[0]


@pytest.mark.asyncio
async def test_chat_completions_synthetic_tool_is_intercepted_and_mixing_is_rejected() -> None:
    captured: list[dict[str, Any]] = []
    responses = iter(
        [
            _completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "submit-1",
                            "type": "function",
                            "function": {
                                "name": "__oria_submit_response__",
                                "arguments": '{"answer":"ok"}',
                            },
                        }
                    ],
                }
            ),
            _completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "submit-2",
                            "type": "function",
                            "function": {
                                "name": "__oria_submit_response__",
                                "arguments": '{"answer":"ok"}',
                            },
                        },
                        {
                            "id": "business-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                        },
                    ],
                }
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatProvider(_profile(mode="synthetic_tool"), client)
        options = ChatOptions(response_schema=_schema())
        result = await provider.chat(
            [Message(role="user", content="answer")],
            None,
            options=options,  # type: ignore[arg-type]
        )
        with pytest.raises(StructuredOutputError, match="mixed"):
            await provider.chat(
                [Message(role="user", content="mixed")],
                None,  # type: ignore[arg-type]
                tools=[_tool()],
                options=options,
            )

    assert result.structured_output == {"answer": "ok"}
    synthetic = captured[0]["tools"][0]
    assert synthetic["function"]["name"] == "__oria_submit_response__"
    assert "response_format" not in captured[0]


@pytest.mark.asyncio
async def test_chat_completions_stream_maps_deltas_and_done_monotonically() -> None:
    chunks = [
        {
            "id": "chatcmpl-stream",
            "choices": [
                {
                    "delta": {"reasoning_content": "private", "content": "hello"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": '{"id":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream",
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"1"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {"id": "chatcmpl-stream", "choices": [], "usage": _usage()},
    ]
    body = "\n\n".join([*(f"data: {json.dumps(chunk)}" for chunk in chunks), "data: [DONE]"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in OpenAICompatProvider(_profile(), client).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
            )
        ]

    assert [type(event) for event in events] == [
        ReasoningDelta,
        TextDelta,
        ToolCallDelta,
        ToolCallDelta,
        UsageDelta,
        Done,
    ]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[0].internal_text() == "private"
    assert events[1].text == "hello"
    assert events[2].tool_call_id == "call-1"
    assert events[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_chat_completions_unsupported_and_missing_key_fail_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UnsupportedCapabilityError):
            await OpenAICompatProvider(_profile(mode="unsupported"), client).chat(
                [Message(role="user", content="answer")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )
        with pytest.raises(AuthenticationError):
            await OpenAICompatProvider(_profile(key=None), client).chat(
                [Message(role="user", content="answer")],
                None,  # type: ignore[arg-type]
            )

    assert called is False


@pytest.mark.asyncio
async def test_chat_completions_structured_stream_rejects_invalid_json() -> None:
    chunks = [
        {
            "id": "chatcmpl-stream",
            "choices": [{"delta": {"content": "not-json"}, "finish_reason": "stop"}],
        },
        {"id": "chatcmpl-stream", "choices": [], "usage": _usage()},
    ]
    body = "\n\n".join([*(f"data: {json.dumps(chunk)}" for chunk in chunks), "data: [DONE]"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://provider.invalid/v1", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in OpenAICompatProvider(_profile(), client).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )
        ]

    assert [type(event) for event in events] == [UsageDelta, ProviderError]
    assert events[-1].code == "structured_output_error"
