"""Provider, structured-output, streaming, and runtime assembly contracts for T04."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from oria.config import resolve_runtime_config
from oria.config.models import ResolvedLLMProfile
from oria.core.runtime import build_runtime
from oria.core.types import (
    ChatOptions,
    ChatResult,
    Done,
    Message,
    ProviderError,
    ReasoningDelta,
    ResponseSchema,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolSpec,
    Usage,
    UsageDelta,
)
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.embeddings import FixtureEmbedder
from oria.providers.errors import (
    InvalidRequestError,
    ProviderResponseError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)
from oria.providers.mock import MockLLMProvider
from oria.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.contract


def _profile(*, mode: str = "native_json_schema") -> ResolvedLLMProfile:
    return ResolvedLLMProfile.model_validate(
        {
            "profile_id": "deepseek",
            "provider": "deepseek",
            "api_dialect": "responses",
            "model": "deepseek-v4-flash",
            "api_key": "test-key",
            "base_url": "https://api.deepseek.com",
            "structured_output_mode": mode,
        }
    )


def _schema() -> ResponseSchema:
    return ResponseSchema(
        name="campaign_proposal",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        strict=True,
    )


def _response(*output: dict[str, object]) -> dict[str, object]:
    return {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "model": "deepseek-v4-flash",
        "output": list(output),
        "usage": {
            "input_tokens": 7,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


async def _runtime_context(tmp_path: Path):
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=tmp_path / "data"))
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="provider-session",
        thread_id="provider-thread",
        run_id="provider-run",
    )
    return runtime, ctx


@pytest.mark.asyncio
async def test_runtime_assembles_mock_provider_and_fixture_embedder(tmp_path: Path) -> None:
    runtime, ctx = await _runtime_context(tmp_path)
    try:
        assert isinstance(ctx.llm, MockLLMProvider)
        assert isinstance(ctx.embedder, FixtureEmbedder)
        assert (await ctx.llm.capabilities(ctx)).api_dialect == "mock"
        assert len(await ctx.embedder.embed(["招商规则"], ctx)) == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_deepseek_responses_maps_native_schema_tools_and_usage(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=_response(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"answer":"ok"}'}],
                }
            ),
        )

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            provider = OpenAICompatProvider(_profile(), client)
            result = await provider.chat(
                [Message(role="user", content="生成提案")],
                ctx,
                tools=[
                    ToolSpec(
                        name="query_merchants",
                        schema_version=1,
                        description="查询商家",
                        json_schema={"type": "object", "properties": {}},
                    )
                ],
                options=ChatOptions(response_schema=_schema(), max_output_tokens=128),
            )
        finally:
            await runtime.aclose()

    assert result.structured_output == {"answer": "ok"}
    assert result.content == ()
    assert result.usage.input_tokens == 7
    assert result.usage.reasoning_tokens == 1
    assert result.usage.cache_read_tokens == 2
    assert result.request_id == "resp-1"
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/responses"
    assert request.headers["authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "campaign_proposal",
        "schema": _schema().model_dump(mode="json")["json_schema"],
    }
    assert "response_format" not in payload
    assert payload["tools"][0]["name"] == "query_merchants"


@pytest.mark.asyncio
async def test_native_structured_output_is_locally_validated(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"answer":1}'}],
                }
            ),
        )

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            with pytest.raises(StructuredOutputError) as excinfo:
                await OpenAICompatProvider(_profile(), client).chat(
                    [Message(role="user", content="invalid")],
                    ctx,
                    options=ChatOptions(response_schema=_schema()),
                )
        finally:
            await runtime.aclose()

    assert excinfo.value.provider_request_id == "resp-1"
    assert excinfo.value.provider_model == "deepseek-v4-flash"
    assert excinfo.value.usage == Usage(
        input_tokens=7,
        output_tokens=5,
        cache_read_tokens=2,
        reasoning_tokens=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": "7", "output_tokens": 5},
        {"input_tokens": 7, "output_tokens": -1},
    ],
)
async def test_invalid_or_missing_provider_usage_fails_closed(
    tmp_path: Path, usage: object
) -> None:
    body = _response(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": '{"answer":"ok"}'}],
        }
    )
    body["usage"] = usage

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            with pytest.raises(ProviderResponseError) as excinfo:
                await OpenAICompatProvider(_profile(), client).chat(
                    [Message(role="user", content="invalid usage")],
                    ctx,
                    options=ChatOptions(response_schema=_schema()),
                )
        finally:
            await runtime.aclose()

    assert excinfo.value.provider_request_id == "resp-1"


@pytest.mark.asyncio
async def test_native_schema_allows_business_tool_only_turn(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response(
                {
                    "type": "function_call",
                    "call_id": "rules-1",
                    "name": "search_campaign_rules",
                    "arguments": (
                        '{"intent":"merchant_recruitment",'
                        '"effective_at":"2026-07-15T00:00:00+08:00"}'
                    ),
                }
            ),
        )

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            result = await OpenAICompatProvider(_profile(), client).chat(
                [Message(role="user", content="先查规则")],
                ctx,
                tools=[
                    ToolSpec(
                        name="search_campaign_rules",
                        schema_version=1,
                        description="查询规则",
                        json_schema={"type": "object", "properties": {}},
                    )
                ],
                options=ChatOptions(response_schema=_schema()),
            )
        finally:
            await runtime.aclose()

    assert result.structured_output is None
    assert [call.name for call in result.tool_calls] == ["search_campaign_rules"]


@pytest.mark.asyncio
async def test_synthetic_tool_is_intercepted_and_cannot_mix_with_business_tools(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            _response(
                {
                    "type": "function_call",
                    "call_id": "submit-1",
                    "name": "__oria_submit_response__",
                    "arguments": '{"answer":"ok"}',
                }
            ),
            _response(
                {
                    "type": "function_call",
                    "call_id": "submit-2",
                    "name": "__oria_submit_response__",
                    "arguments": '{"answer":"ok"}',
                },
                {
                    "type": "function_call",
                    "call_id": "business-1",
                    "name": "query_merchants",
                    "arguments": "{}",
                },
            ),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            provider = OpenAICompatProvider(_profile(mode="synthetic_tool"), client)
            options = ChatOptions(response_schema=_schema())
            first = await provider.chat(
                [Message(role="user", content="valid")], ctx, options=options
            )
            assert first.structured_output == {"answer": "ok"}
            assert first.tool_calls == ()
            with pytest.raises(StructuredOutputError, match="mixed"):
                await provider.chat(
                    [Message(role="user", content="mixed")],
                    ctx,
                    tools=[
                        ToolSpec(
                            name="query_merchants",
                            schema_version=1,
                            description="查询",
                            json_schema={"type": "object", "properties": {}},
                        )
                    ],
                    options=options,
                )
        finally:
            await runtime.aclose()


@pytest.mark.asyncio
async def test_unsupported_structured_output_fails_before_network(tmp_path: Path) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            provider = OpenAICompatProvider(_profile(mode="unsupported"), client)
            with pytest.raises(UnsupportedCapabilityError):
                await provider.chat(
                    [Message(role="user", content="unsupported")],
                    ctx,
                    options=ChatOptions(response_schema=_schema()),
                )
        finally:
            await runtime.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_responses_stream_maps_semantic_events_monotonically(tmp_path: Path) -> None:
    completed = _response()
    body = "\n".join(
        [
            "event: response.reasoning_text.delta",
            'data: {"type":"response.reasoning_text.delta","sequence_number":1,"delta":"secret"}',
            "",
            "event: response.output_text.delta",
            'data: {"type":"response.output_text.delta","sequence_number":2,"delta":"hello"}',
            "",
            "event: response.output_item.added",
            "data: "
            + json.dumps(
                {
                    "type": "response.output_item.added",
                    "sequence_number": 3,
                    "item": {
                        "id": "fc-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "query_merchants",
                    },
                }
            ),
            "",
            "event: response.function_call_arguments.delta",
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 4,
                    "item_id": "fc-1",
                    "delta": "{}",
                }
            ),
            "",
            "event: response.completed",
            "data: " + json.dumps({"type": "response.completed", "response": completed}),
            "",
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            events = [
                event
                async for event in OpenAICompatProvider(_profile(), client).chat_stream(
                    [Message(role="user", content="stream")], ctx
                )
            ]
        finally:
            await runtime.aclose()

    assert [type(event) for event in events] == [
        ReasoningDelta,
        TextDelta,
        ToolCallDelta,
        UsageDelta,
        Done,
    ]
    assert [event.sequence for event in events] == list(range(5))
    assert events[0].model_dump() == {
        "type": "reasoning_delta",
        "sequence": 0,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "request_id": None,
    }
    assert events[0].internal_text() == "secret"


@pytest.mark.asyncio
async def test_native_structured_stream_is_buffered_and_locally_validated(tmp_path: Path) -> None:
    completed = _response()
    body = "\n".join(
        [
            'data: {"type":"response.output_text.delta","delta":"{\\"answer\\":1}"}',
            "",
            "data: " + json.dumps({"type": "response.completed", "response": completed}),
            "",
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            events = [
                event
                async for event in OpenAICompatProvider(_profile(), client).chat_stream(
                    [Message(role="user", content="stream")],
                    ctx,
                    options=ChatOptions(response_schema=_schema()),
                )
            ]
        finally:
            await runtime.aclose()

    assert [type(event) for event in events] == [UsageDelta, ProviderError]
    assert events[0].request_id == "resp-1"
    assert events[1].request_id == "resp-1"
    assert events[1].code == "structured_output_error"


@pytest.mark.asyncio
async def test_synthetic_structured_stream_never_emits_reserved_tool_delta(
    tmp_path: Path,
) -> None:
    completed = _response()
    body = "\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "item-submit",
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "__oria_submit_response__",
                    },
                }
            ),
            "",
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "item-submit",
                    "delta": '{"answer":"ok"}',
                }
            ),
            "",
            "data: " + json.dumps({"type": "response.completed", "response": completed}),
            "",
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            events = [
                event
                async for event in OpenAICompatProvider(
                    _profile(mode="synthetic_tool"), client
                ).chat_stream(
                    [Message(role="user", content="stream")],
                    ctx,
                    options=ChatOptions(response_schema=_schema()),
                )
            ]
        finally:
            await runtime.aclose()

    assert [type(event) for event in events] == [TextDelta, UsageDelta, Done]
    assert events[0].text == '{"answer":"ok"}'


@pytest.mark.asyncio
async def test_synthetic_structured_stream_rejects_business_tool_mix(tmp_path: Path) -> None:
    completed = _response()
    events_payload = [
        {
            "type": "response.output_item.added",
            "item": {
                "id": "item-submit",
                "type": "function_call",
                "call_id": "call-submit",
                "name": "__oria_submit_response__",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-submit",
            "delta": '{"answer":"ok"}',
        },
        {
            "type": "response.output_item.added",
            "item": {
                "id": "item-business",
                "type": "function_call",
                "call_id": "call-business",
                "name": "query_merchants",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-business",
            "delta": "{}",
        },
        {"type": "response.completed", "response": completed},
    ]
    body = "\n\n".join(f"data: {json.dumps(event)}" for event in events_payload)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            events = [
                event
                async for event in OpenAICompatProvider(
                    _profile(mode="synthetic_tool"), client
                ).chat_stream(
                    [Message(role="user", content="stream")],
                    ctx,
                    tools=[
                        ToolSpec(
                            name="query_merchants",
                            schema_version=1,
                            description="query",
                            json_schema={"type": "object", "properties": {}},
                        )
                    ],
                    options=ChatOptions(response_schema=_schema()),
                )
            ]
        finally:
            await runtime.aclose()

    assert [type(event) for event in events] == [UsageDelta, ProviderError]
    assert events[0].request_id == "resp-1"
    assert events[1].request_id == "resp-1"
    assert events[1].code == "structured_output_error"


@pytest.mark.asyncio
async def test_strict_schema_rejects_unknown_fields_even_when_schema_allows_them(
    tmp_path: Path,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"answer":"ok","extra":true}'}],
                }
            ),
        )

    schema = ResponseSchema(
        name="strict_response",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": True,
        },
        strict=True,
    )
    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            with pytest.raises(StructuredOutputError):
                await OpenAICompatProvider(_profile(), client).chat(
                    [Message(role="user", content="strict")],
                    ctx,
                    options=ChatOptions(response_schema=schema),
                )
        finally:
            await runtime.aclose()


@pytest.mark.asyncio
async def test_schema_tool_name_collision_fails_before_network(tmp_path: Path) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            with pytest.raises(InvalidRequestError, match="conflicts"):
                await OpenAICompatProvider(_profile(), client).chat(
                    [Message(role="user", content="collision")],
                    ctx,
                    tools=[
                        ToolSpec(
                            name="campaign_proposal",
                            schema_version=1,
                            description="collision",
                            json_schema={"type": "object", "properties": {}},
                        )
                    ],
                    options=ChatOptions(response_schema=_schema()),
                )
        finally:
            await runtime.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_assistant_tool_history_is_mapped_and_diagnostics_are_redacted(
    tmp_path: Path,
) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        body = _response(
            {"type": "reasoning", "content": "private chain"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        )
        body["token"] = "secret-echo"
        return httpx.Response(200, json=body)

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            result = await OpenAICompatProvider(_profile(), client).chat(
                [
                    Message(
                        role="assistant",
                        content=(ToolCallBlock(id="call-1", name="query_merchants", args={}),),
                    ),
                    Message(role="tool", tool_call_id="call-1", content="{}"),
                ],
                ctx,
            )
        finally:
            await runtime.aclose()

    assert captured[0]["input"] == [
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "query_merchants",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
    ]
    diagnostic = str(result.internal_raw_response())
    assert "secret-echo" not in diagnostic
    assert "private chain" not in diagnostic


@pytest.mark.asyncio
async def test_stream_without_terminal_event_emits_monotonic_error(tmp_path: Path) -> None:
    body = "\n".join(
        [
            "event: response.output_text.delta",
            'data: {"type":"response.output_text.delta","delta":"partial"}',
            "",
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    runtime, ctx = await _runtime_context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            events = [
                event
                async for event in OpenAICompatProvider(_profile(), client).chat_stream(
                    [Message(role="user", content="stream")], ctx
                )
            ]
        finally:
            await runtime.aclose()

    assert [type(event) for event in events] == [TextDelta, ProviderError]
    assert [event.sequence for event in events] == [0, 1]
    assert events[1].code == "incomplete_stream"


@pytest.mark.asyncio
async def test_default_mock_generates_values_for_common_strict_schema(tmp_path: Path) -> None:
    schema = ResponseSchema(
        name="strict_fixture",
        json_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 3},
                "scores": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "integer", "minimum": 1},
                },
                "profile": {"$ref": "#/$defs/profile"},
            },
            "required": ["name", "scores", "profile"],
            "$defs": {
                "profile": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "minLength": 1}},
                    "required": ["code"],
                }
            },
        },
    )
    runtime, ctx = await _runtime_context(tmp_path)
    try:
        result = await MockLLMProvider().chat(
            [Message(role="user", content="fixture")],
            ctx,
            options=ChatOptions(response_schema=schema),
        )
    finally:
        await runtime.aclose()

    assert result.structured_output == {
        "name": "xxx",
        "scores": [1, 1],
        "profile": {"code": "x"},
    }


@pytest.mark.asyncio
async def test_strict_schema_preserves_typed_dynamic_map_entries(tmp_path: Path) -> None:
    schema = ResponseSchema(
        name="citation_map",
        json_schema={
            "type": "object",
            "properties": {
                "field_evidence": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"document_id": {"type": "string"}},
                        "required": ["document_id"],
                    },
                }
            },
            "required": ["field_evidence"],
        },
    )
    runtime, ctx = await _runtime_context(tmp_path)
    try:
        fixed = ChatResult(
            content=(),
            tool_calls=(),
            structured_output={"field_evidence": {"basic.title": {"document_id": "doc-1"}}},
            usage=Usage(input_tokens=0, output_tokens=0),
            finish_reason="stop",
        )
        result = await MockLLMProvider(fixed).chat(
            [Message(role="user", content="fixture")],
            ctx,
            options=ChatOptions(response_schema=schema),
        )
    finally:
        await runtime.aclose()

    assert result.structured_output == {"field_evidence": {"basic.title": {"document_id": "doc-1"}}}
