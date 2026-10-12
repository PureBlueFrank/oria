from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oria.config.models import ResolvedLLMProfile
from oria.core.types import ChatOptions, Message, ResponseSchema, ToolSpec, Usage
from oria.providers.codex_app_server import (
    CodexAppServerProvider,
    _parse_envelope,
    _turn_prompt,
    _usage,
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_tenant", "other_subject"),
    [("tenant-b", "subject-a"), ("tenant-a", "subject-b")],
)
async def test_provider_threads_are_isolated_and_followups_reused(
    tmp_path: Path, monkeypatch, other_tenant: str, other_subject: str
) -> None:
    provider = CodexAppServerProvider(
        ResolvedLLMProfile(
            profile_id="synthetic-codex",
            provider="codex",
            api_dialect="responses",
            model="synthetic-model",
        ),
        work_dir=tmp_path,
    )
    start = AsyncMock(side_effect=["model-thread-a", "model-thread-b"])
    request = AsyncMock(return_value={"turn": {"id": "synthetic-turn"}})
    result = json.dumps(
        {
            "action": "final",
            "tool_calls": [],
            "final_response_json": '{"answer":"synthetic"}',
            "text": "",
        }
    )
    monkeypatch.setattr(provider, "_start_thread", start)
    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(
        provider,
        "_read_turn",
        AsyncMock(return_value=(result, Usage(input_tokens=0, output_tokens=0), None, None)),
    )
    first = SimpleNamespace(
        tenant_id="tenant-a",
        actor=SimpleNamespace(subject_id="subject-a"),
        thread_id="shared-external-thread",
    )
    other = SimpleNamespace(
        tenant_id=other_tenant,
        actor=SimpleNamespace(subject_id=other_subject),
        thread_id="shared-external-thread",
    )
    options = ChatOptions(response_schema=_response_schema())
    first_question = Message(role="user", content="first synthetic question")
    other_question = Message(role="user", content="other synthetic question")
    followup = [
        Message(role="assistant", content="synthetic answer"),
        Message(role="user", content="first synthetic followup"),
    ]
    await provider.chat([first_question], first, options=options)
    await provider.chat([other_question], other, options=options)
    await provider.chat([first_question, *followup], first, options=options)

    sent = [call.args[1] for call in request.await_args_list]
    assert sent[0]["threadId"] != sent[1]["threadId"]
    assert sent[0]["threadId"] == sent[2]["threadId"]
    assert start.await_count == 2
    for turn, expected in zip(sent, ([first_question], [other_question], followup), strict=True):
        text = turn["input"][0]["text"]
        messages_json = text.split("ORIA_MESSAGES_JSON:\n", 1)[1].split(
            "\nAVAILABLE_ORIA_TOOLS_JSON:", 1
        )[0]
        assert json.loads(messages_json) == [
            message.model_dump(mode="json") for message in expected
        ]
