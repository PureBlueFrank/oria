"""Unified V02-PROV-01/V02-PROV-03 adapter and strategy matrix."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from oria.config import resolve_runtime_config
from oria.config.models import ResolvedLLMProfile
from oria.core.types import (
    ChatOptions,
    ChatResult,
    Done,
    Message,
    ProviderError,
    ReasoningDelta,
    ResponseSchema,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallBlock,
    ToolCallDelta,
    ToolSpec,
    Usage,
    UsageDelta,
)
from oria.providers.anthropic import AnthropicProvider
from oria.providers.errors import (
    AuthenticationError,
    ProviderTimeoutError,
    ProviderUnavailable,
    RateLimitError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)
from oria.providers.mock import MockLLMProvider
from oria.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.contract

ProviderFactory = Callable[[httpx.AsyncClient], OpenAICompatProvider | AnthropicProvider]


def _profile(
    provider: str,
    dialect: str,
    *,
    mode: str = "native_json_schema",
    key: str | None = "fixture-key",
) -> ResolvedLLMProfile:
    return ResolvedLLMProfile.model_validate(
        {
            "profile_id": provider,
            "provider": provider,
            "api_dialect": dialect,
            "model": f"{provider}-fixture-model",
            "api_key": key,
            "base_url": "https://provider.invalid",
            "structured_output_mode": mode,
        }
    )


def _provider(
    client: httpx.AsyncClient,
    provider: str,
    dialect: str,
    *,
    mode: str = "native_json_schema",
    key: str | None = "fixture-key",
) -> OpenAICompatProvider | AnthropicProvider:
    profile = _profile(provider, dialect, mode=mode, key=key)
    if dialect == "anthropic_messages":
        return AnthropicProvider(profile, client)
    return OpenAICompatProvider(profile, client)


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
        description="Lookup a fixture.",
        json_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )


def _responses_body(*output: dict[str, object]) -> dict[str, object]:
    return {
        "id": "resp-1",
        "model": "responses-fixture-model",
        "status": "completed",
        "output": list(output),
        "usage": {
            "input_tokens": 12,
            "output_tokens": 6,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }


def _chat_body(message: dict[str, object]) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "model": "chat-fixture-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def _anthropic_body(*content: dict[str, object]) -> dict[str, object]:
    return {
        "id": "msg-1",
        "model": "anthropic-fixture-model",
        "content": list(content),
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 6,
            "cache_read_input_tokens": 3,
            "reasoning_tokens": 2,
        },
    }


PROFILE_CASES = [
    ("deepseek", "responses", "native_json_schema"),
    ("kimi", "chat_completions", "synthetic_tool"),
    ("zhipu", "chat_completions", "synthetic_tool"),
    ("openai", "chat_completions", "native_json_schema"),
    ("anthropic", "anthropic_messages", "native_json_schema"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_name", "dialect", "mode"), PROFILE_CASES)
async def test_real_profiles_share_capability_contract(
    provider_name: str, dialect: str, mode: str
) -> None:
    async with httpx.AsyncClient(base_url="https://provider.invalid") as client:
        adapter = _provider(client, provider_name, dialect, mode=mode)
        capabilities = await adapter.capabilities(None)  # type: ignore[arg-type]

    assert capabilities.api_dialect == dialect
    assert capabilities.structured_output_modes == frozenset({mode})
    assert capabilities.tool_calling is True
    assert capabilities.streaming is True


@pytest.mark.asyncio
async def test_mock_shares_capability_result_and_stream_contract() -> None:
    fixed = ChatResult(
        content=(
            TextBlock(text="visible"),
            ToolCallBlock(id="call-1", name="lookup", args={"id": "1"}),
        ),
        tool_calls=(ToolCall(id="call-1", name="lookup", args={"id": "1"}),),
        usage=Usage(input_tokens=12, output_tokens=6),
        finish_reason="stop",
        request_id="mock-1",
    )
    adapter = MockLLMProvider(fixed)
    capabilities = await adapter.capabilities(None)  # type: ignore[arg-type]
    result = await adapter.chat([Message(role="user", content="question")], None)  # type: ignore[arg-type]
    events = [
        event
        async for event in adapter.chat_stream(
            [Message(role="user", content="question")],
            None,  # type: ignore[arg-type]
        )
    ]

    assert capabilities.api_dialect == "mock"
    assert result.text == "visible"
    assert result.tool_calls[0].name == "lookup"
    assert [type(event) for event in events] == [
        TextDelta,
        ToolCallDelta,
        UsageDelta,
        Done,
    ]
    assert [event.sequence for event in events] == list(range(4))


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_name", "dialect", "_mode"), PROFILE_CASES)
async def test_real_adapters_share_text_tool_usage_and_reasoning_separation(
    provider_name: str, dialect: str, _mode: str
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        if dialect == "responses":
            body = _responses_body(
                {"type": "reasoning", "content": "private"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "visible"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"id":"1"}',
                },
            )
        elif dialect == "chat_completions":
            body = _chat_body(
                {
                    "role": "assistant",
                    "content": "visible",
                    "reasoning_content": "private",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                        }
                    ],
                }
            )
        else:
            body = _anthropic_body(
                {"type": "text", "text": "visible"},
                {"type": "thinking", "thinking": "private"},
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"id": "1"}},
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        result = await _provider(client, provider_name, dialect).chat(
            [Message(role="user", content="question")],
            None,  # type: ignore[arg-type]
            tools=[_tool()],
        )

    assert result.text == "visible"
    assert result.tool_calls == (ToolCall(id="call-1", name="lookup", args={"id": "1"}),)
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 6
    assert result.usage.cache_read_tokens == 3
    assert result.usage.reasoning_tokens == 2
    assert "private" not in str(result.content)
    assert "private" not in str(result.internal_raw_response())


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat_completions", "anthropic_messages"])
@pytest.mark.parametrize("mode", ["native_json_schema", "synthetic_tool", "unsupported"])
async def test_endpoint_dialect_by_structured_strategy_matrix(dialect: str, mode: str) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if dialect == "responses":
            if mode == "native_json_schema":
                body = _responses_body(
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"answer":"ok"}'}],
                    }
                )
            else:
                body = _responses_body(
                    {
                        "type": "function_call",
                        "call_id": "submit-1",
                        "name": "__oria_submit_response__",
                        "arguments": '{"answer":"ok"}',
                    }
                )
        elif dialect == "chat_completions":
            if mode == "native_json_schema":
                body = _chat_body({"role": "assistant", "content": '{"answer":"ok"}'})
            else:
                body = _chat_body(
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
                )
        elif mode == "native_json_schema":
            body = _anthropic_body({"type": "text", "text": '{"answer":"ok"}'})
        else:
            body = _anthropic_body(
                {
                    "type": "tool_use",
                    "id": "submit-1",
                    "name": "__oria_submit_response__",
                    "input": {"answer": "ok"},
                }
            )
        return httpx.Response(200, json=body)

    provider_name = "anthropic" if dialect == "anthropic_messages" else "openai"
    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = _provider(client, provider_name, dialect, mode=mode)
        if mode == "unsupported":
            with pytest.raises(UnsupportedCapabilityError):
                await adapter.chat(
                    [Message(role="user", content="answer")],
                    None,  # type: ignore[arg-type]
                    options=ChatOptions(response_schema=_schema()),
                )
        else:
            result = await adapter.chat(
                [Message(role="user", content="answer")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )
            assert result.structured_output == {"answer": "ok"}

    if mode == "unsupported":
        assert captured == []
        return
    request = captured[0]
    payload = json.loads(request.content)
    if dialect == "responses":
        assert request.url.path == "/responses"
        assert "response_format" not in payload
        assert "output_config" not in payload
        if mode == "native_json_schema":
            assert payload["text"]["format"]["type"] == "json_schema"
        else:
            assert payload["tools"][0]["name"] == "__oria_submit_response__"
    elif dialect == "chat_completions":
        assert request.url.path == "/chat/completions"
        assert "text" not in payload
        assert "output_config" not in payload
        if mode == "native_json_schema":
            assert payload["response_format"]["type"] == "json_schema"
        else:
            assert payload["tools"][0]["function"]["name"] == "__oria_submit_response__"
    else:
        assert request.url.path == "/v1/messages"
        assert "text" not in payload
        assert "response_format" not in payload
        if mode == "native_json_schema":
            assert payload["output_config"]["format"]["type"] == "json_schema"
        else:
            assert payload["tools"][0]["name"] == "__oria_submit_response__"


def _invalid_structured_body(dialect: str, text: str) -> dict[str, object]:
    if dialect == "responses":
        return _responses_body(
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        )
    if dialect == "chat_completions":
        return _chat_body({"role": "assistant", "content": text})
    return _anthropic_body({"type": "text", "text": text})


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat_completions", "anthropic_messages"])
@pytest.mark.parametrize("text", ["not-json", '{"answer":1}'])
async def test_native_strategies_reject_invalid_json_and_schema(dialect: str, text: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_invalid_structured_body(dialect, text))

    provider_name = "anthropic" if dialect == "anthropic_messages" else "openai"
    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(StructuredOutputError):
            await _provider(client, provider_name, dialect).chat(
                [Message(role="user", content="answer")],
                None,  # type: ignore[arg-type]
                options=ChatOptions(response_schema=_schema()),
            )


def _mixed_body(dialect: str) -> dict[str, object]:
    if dialect == "responses":
        return _responses_body(
            {
                "type": "function_call",
                "call_id": "submit-1",
                "name": "__oria_submit_response__",
                "arguments": '{"answer":"ok"}',
            },
            {
                "type": "function_call",
                "call_id": "business-1",
                "name": "lookup",
                "arguments": '{"id":"1"}',
            },
        )
    if dialect == "chat_completions":
        return _chat_body(
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
                    },
                    {
                        "id": "business-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                    },
                ],
            }
        )
    return _anthropic_body(
        {
            "type": "tool_use",
            "id": "submit-1",
            "name": "__oria_submit_response__",
            "input": {"answer": "ok"},
        },
        {"type": "tool_use", "id": "business-1", "name": "lookup", "input": {"id": "1"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat_completions", "anthropic_messages"])
async def test_synthetic_strategies_reject_reserved_and_business_tool_mix(dialect: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mixed_body(dialect))

    provider_name = "anthropic" if dialect == "anthropic_messages" else "openai"
    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(StructuredOutputError):
            await _provider(client, provider_name, dialect, mode="synthetic_tool").chat(
                [Message(role="user", content="answer")],
                None,  # type: ignore[arg-type]
                tools=[_tool()],
                options=ChatOptions(response_schema=_schema()),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat_completions", "anthropic_messages"])
@pytest.mark.parametrize(
    ("failure", "expected_type", "retryable"),
    [
        (401, AuthenticationError, False),
        (429, RateLimitError, True),
        (503, ProviderUnavailable, True),
        ("timeout", ProviderTimeoutError, True),
    ],
)
async def test_http_errors_are_stable_safe_and_body_free(
    dialect: str,
    failure: int | str,
    expected_type: type[Exception],
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("private network coordinates", request=request)
        return httpx.Response(
            cast(int, failure),
            text="secret upstream response body",
            headers={"retry-after": "2", "request-id": "upstream-1"},
        )

    provider_name = "anthropic" if dialect == "anthropic_messages" else "openai"
    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(expected_type) as excinfo:
            await _provider(client, provider_name, dialect).chat(
                [Message(role="user", content="failure")],
                None,  # type: ignore[arg-type]
            )

    assert excinfo.value.retryable is retryable
    assert "secret upstream" not in str(excinfo.value)
    assert "network coordinates" not in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "dialect"),
    [
        ("deepseek", "responses"),
        ("kimi", "chat_completions"),
        ("zhipu", "chat_completions"),
        ("openai", "chat_completions"),
        ("anthropic", "anthropic_messages"),
    ],
)
async def test_real_profiles_without_keys_fail_closed_before_network(
    provider_name: str, dialect: str
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AuthenticationError):
            await _provider(client, provider_name, dialect, key=None).chat(
                [Message(role="user", content="question")],
                None,  # type: ignore[arg-type]
            )
    assert called is False


def _responses_stream() -> str:
    events = [
        {"type": "response.reasoning_text.delta", "delta": "private"},
        {"type": "response.output_text.delta", "delta": "visible"},
        {
            "type": "response.output_item.added",
            "item": {
                "id": "item-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": '{"id":"1"}',
        },
        {"type": "response.completed", "response": _responses_body()},
    ]
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events)


def _chat_stream() -> str:
    events = [
        {
            "id": "chat-stream",
            "choices": [
                {
                    "delta": {"reasoning_content": "private", "content": "visible"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chat-stream",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"id":"1"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "chat-stream",
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
    ]
    return "\n\n".join([*(f"data: {json.dumps(event)}" for event in events), "data: [DONE]"])


def _anthropic_stream() -> str:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg-stream", "usage": {"input_tokens": 12, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "private"},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "visible"},
        },
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"id":"1"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 6, "reasoning_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_name", "dialect", "_mode"), PROFILE_CASES)
async def test_real_adapter_streams_share_monotonic_semantic_events(
    provider_name: str, dialect: str, _mode: str
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {
            "responses": _responses_stream,
            "chat_completions": _chat_stream,
            "anthropic_messages": _anthropic_stream,
        }[dialect]()
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in _provider(client, provider_name, dialect).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
                tools=[_tool()],
            )
        ]

    assert [event.sequence for event in events] == list(range(len(events)))
    assert [type(event) for event in events] == [
        ReasoningDelta,
        TextDelta,
        ToolCallDelta,
        UsageDelta,
        Done,
    ]
    assert events[0].internal_text() == "private"
    assert events[1].text == "visible"
    assert events[2].tool_call_id == "call-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat_completions", "anthropic_messages"])
async def test_stream_http_failures_become_safe_provider_error_events(dialect: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="secret upstream stream body")

    provider_name = "anthropic" if dialect == "anthropic_messages" else "openai"
    async with httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        events = [
            event
            async for event in _provider(client, provider_name, dialect).chat_stream(
                [Message(role="user", content="stream")],
                None,  # type: ignore[arg-type]
            )
        ]

    assert len(events) == 1
    assert isinstance(events[0], ProviderError)
    assert events[0].code == "provider_unavailable"
    assert "secret upstream" not in events[0].safe_message


@pytest.mark.parametrize(
    ("profile_name", "environment", "expected_dialect", "expected_mode"),
    [
        ("deepseek", {}, "responses", "native_json_schema"),
        (
            "kimi",
            {"MOONSHOT_MODEL": "kimi-fixture", "MOONSHOT_API_KEY": "key"},
            "chat_completions",
            "synthetic_tool",
        ),
        (
            "zhipu",
            {"ZHIPU_MODEL": "zhipu-fixture", "ZHIPU_API_KEY": "key"},
            "chat_completions",
            "synthetic_tool",
        ),
        (
            "openai",
            {"OPENAI_MODEL": "openai-fixture", "OPENAI_API_KEY": "key"},
            "chat_completions",
            "native_json_schema",
        ),
        (
            "anthropic",
            {"ANTHROPIC_MODEL": "claude-fixture", "ANTHROPIC_API_KEY": "key"},
            "anthropic_messages",
            "native_json_schema",
        ),
    ],
)
def test_default_real_profiles_resolve_to_explicit_dialect_and_strategy(
    tmp_path: Path,
    profile_name: str,
    environment: dict[str, str],
    expected_dialect: str,
    expected_mode: str,
) -> None:
    if profile_name == "deepseek":
        environment = {"DEEPSEEK_API_KEY": "key"}
    resolved = resolve_runtime_config(
        llm_profile=profile_name,
        environ=environment,
        data_dir=tmp_path / "data",
    )

    assert resolved.llm.api_dialect == expected_dialect
    assert resolved.llm.structured_output_mode == expected_mode


def test_v02_provider_status_cards_are_initialized_without_live_claims() -> None:
    path = Path("reports/verification/v0.2/provider-status.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload["profiles"]) == {
        "mock",
        "deepseek",
        "kimi",
        "zhipu",
        "openai",
        "anthropic",
    }
    assert all(card["live_verified"] is False for card in payload["profiles"].values())
    assert all(card["network_executed"] is False for card in payload["profiles"].values())
    assert all(card["fixture_contract_verified"] is True for card in payload["profiles"].values())
