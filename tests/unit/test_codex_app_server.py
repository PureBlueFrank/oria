from __future__ import annotations

import json

import pytest

from oria.core.types import ChatOptions, ResponseSchema, ToolSpec
from oria.providers.codex_app_server import _parse_envelope, _turn_prompt, _usage
from oria.providers.errors import StructuredOutputError


def _response_schema() -> ResponseSchema:
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
        name="query_funnel",
        schema_version=1,
        description="Query funnel evidence",
        json_schema={
            "type": "object",
            "properties": {"dimension": {"type": "string"}},
            "required": ["dimension"],
        },
    )


def test_codex_envelope_maps_only_allowlisted_tool_calls() -> None:
    payload = json.dumps(
        {
            "action": "call_tools",
            "tool_calls": [{"name": "query_funnel", "arguments_json": '{"dimension":"region"}'}],
            "final_response_json": "",
            "text": "",
        }
    )

    calls, structured, text = _parse_envelope(
        payload,
        tools=[_tool()],
        options=ChatOptions(response_schema=_response_schema()),
    )

    assert structured is None
    assert text == ""
    assert len(calls) == 1
    assert calls[0].name == "query_funnel"
    assert calls[0].args == {"dimension": "region"}
    assert calls[0].id.startswith("codex_")


def test_codex_envelope_validates_final_response_with_oria_schema() -> None:
    payload = json.dumps(
        {
            "action": "final",
            "tool_calls": [],
            "final_response_json": '{"answer":"grounded"}',
            "text": "",
        }
    )

    calls, structured, _ = _parse_envelope(
        payload,
        tools=[],
        options=ChatOptions(response_schema=_response_schema(), tool_choice="none"),
    )

    assert calls == ()
    assert structured == {"answer": "grounded"}


def test_codex_envelope_rejects_unavailable_tool() -> None:
    payload = json.dumps(
        {
            "action": "call_tools",
            "tool_calls": [{"name": "read_labels", "arguments_json": "{}"}],
            "final_response_json": "",
            "text": "",
        }
    )

    with pytest.raises(StructuredOutputError, match="unavailable tool"):
        _parse_envelope(
            payload,
            tools=[_tool()],
            options=ChatOptions(response_schema=_response_schema()),
        )


def test_codex_prompt_forces_finalization_without_business_tools() -> None:
    prompt = _turn_prompt(
        [],
        [],
        ChatOptions(response_schema=_response_schema(), tool_choice="none"),
    )

    assert "Finalization is mandatory" in prompt
    assert "FINAL_RESPONSE_SCHEMA_JSON" in prompt


def test_codex_usage_preserves_cached_and_reasoning_tokens() -> None:
    usage = _usage(
        {
            "inputTokens": 120,
            "cachedInputTokens": 100,
            "cacheWriteInputTokens": 3,
            "outputTokens": 20,
            "reasoningOutputTokens": 7,
        }
    )

    assert usage.input_tokens == 120
    assert usage.cache_read_tokens == 100
    assert usage.cache_write_tokens == 3
    assert usage.output_tokens == 20
    assert usage.reasoning_tokens == 7
