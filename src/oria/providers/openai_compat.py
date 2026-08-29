"""OpenAI-compatible adapter with explicit Responses and Chat Completions dialects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

import httpx

from oria.config.models import ResolvedLLMProfile
from oria.core.types import (
    ChatOptions,
    ChatResult,
    Done,
    JsonValue,
    Message,
    ProviderCapabilities,
    ProviderError,
    ProviderExtensionBlock,
    ReasoningDelta,
    RefusalBlock,
    ResponseSchema,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallBlock,
    ToolCallDelta,
    ToolResultBlock,
    ToolSpec,
    Usage,
    UsageDelta,
)
from oria.providers.errors import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    ProviderException,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailable,
    RateLimitError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)
from oria.providers.structured import RESERVED_RESPONSE_TOOL, parse_structured_text

if TYPE_CHECKING:
    from oria.core.context import Context


class OpenAICompatProvider:
    """Normalize one resolved OpenAI-compatible model profile into Oria contracts."""

    def __init__(self, profile: ResolvedLLMProfile, client: httpx.AsyncClient) -> None:
        self._profile = profile
        self._client = client

    async def capabilities(self, ctx: Context) -> ProviderCapabilities:
        del ctx
        modes = (
            frozenset()
            if self._profile.structured_output_mode == "unsupported"
            else frozenset({self._profile.structured_output_mode})
        )
        return ProviderCapabilities(
            tool_calling=True,
            streaming=True,
            reasoning=True,
            structured_output=bool(modes),
            parallel_tool_calls=True,
            structured_output_modes=modes,
            api_dialect=self._profile.api_dialect,
        )

    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        del ctx
        selected_options = options or ChatOptions()
        payload = self._request_payload(messages, tools or [], selected_options, stream=False)
        request_kwargs: dict[str, Any] = {}
        if selected_options.timeout_seconds is not None:
            request_kwargs["timeout"] = selected_options.timeout_seconds
        try:
            response = await self._client.post(
                self._endpoint(),
                json=payload,
                headers=self._headers(),
                **request_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("provider request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("provider request failed", retryable=True) from exc
        self._raise_for_status(response)
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderResponseError("provider returned invalid JSON", retryable=False) from exc
        if not isinstance(body, dict):
            raise ProviderResponseError("provider returned an invalid response", retryable=False)
        typed_body = cast(dict[str, Any], body)
        request_id = _optional_string(typed_body.get("id"))
        provider_model = _optional_string(typed_body.get("model"))
        usage = self._parse_usage(typed_body.get("usage"), request_id=request_id)
        try:
            if self._profile.api_dialect == "responses":
                return self._parse_response(typed_body, selected_options, usage=usage)
            if self._profile.api_dialect == "chat_completions":
                return self._parse_chat_completion(typed_body, selected_options, usage=usage)
            raise UnsupportedCapabilityError(
                "configured API dialect is not implemented", retryable=False
            )
        except StructuredOutputError as exc:
            raise StructuredOutputError(
                exc.safe_message,
                retryable=exc.retryable,
                retry_after=exc.retry_after,
                provider_request_id=request_id,
                provider_model=provider_model,
                usage=usage,
            ) from exc

    async def chat_stream(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del ctx
        selected_options = options or ChatOptions()
        if self._profile.api_dialect == "chat_completions":
            async for event in self._chat_completions_stream(
                messages,
                tools or [],
                selected_options,
            ):
                yield event
            return
        sequence = 0
        try:
            payload = self._request_payload(messages, tools or [], selected_options, stream=True)
            request_kwargs: dict[str, Any] = {}
            if selected_options.timeout_seconds is not None:
                request_kwargs["timeout"] = selected_options.timeout_seconds
            async with self._client.stream(
                "POST",
                self._endpoint(),
                json=payload,
                headers=self._headers(),
                **request_kwargs,
            ) as response:
                self._raise_for_status(response)
                item_calls: dict[str, str] = {}
                item_names: dict[str, str] = {}
                structured_text_parts: list[str] = []
                structured_arguments: dict[str, list[str]] = {}
                response_schema = selected_options.response_schema
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(
                            "provider stream returned invalid JSON", retryable=False
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type == "response.output_item.added":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            item_id = item.get("id")
                            call_id = item.get("call_id")
                            name = item.get("name")
                            if (
                                isinstance(item_id, str)
                                and isinstance(call_id, str)
                                and isinstance(name, str)
                            ):
                                item_calls[item_id] = call_id
                                item_names[item_id] = name
                    elif event_type == "response.reasoning_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            yield ReasoningDelta(**self._event_base(sequence), text=delta)
                            sequence += 1
                    elif event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            if response_schema is None:
                                yield TextDelta(**self._event_base(sequence), text=delta)
                                sequence += 1
                            else:
                                structured_text_parts.append(delta)
                    elif event_type == "response.function_call_arguments.delta":
                        item_id = event.get("item_id")
                        delta = event.get("delta")
                        if isinstance(item_id, str) and isinstance(delta, str):
                            if response_schema is None:
                                yield ToolCallDelta(
                                    **self._event_base(sequence),
                                    tool_call_id=item_calls.get(item_id, item_id),
                                    arguments_delta=delta,
                                )
                                sequence += 1
                            else:
                                structured_arguments.setdefault(item_id, []).append(delta)
                    elif event_type == "response.completed":
                        completed = event.get("response")
                        if not isinstance(completed, dict):
                            raise ProviderResponseError(
                                "provider stream completion is invalid", retryable=False
                            )
                        request_id = _optional_string(completed.get("id"))
                        usage = _parse_usage(completed.get("usage"), request_id=request_id)
                        if response_schema is not None:
                            try:
                                structured_text = self._validated_stream_text(
                                    response_schema,
                                    structured_text_parts,
                                    structured_arguments,
                                    item_names,
                                )
                            except StructuredOutputError as exc:
                                yield UsageDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    usage=usage,
                                )
                                sequence += 1
                                yield ProviderError(
                                    **self._event_base(sequence, request_id=request_id),
                                    code=exc.code,
                                    safe_message=exc.safe_message,
                                    retryable=exc.retryable,
                                )
                                return
                            if structured_text is not None:
                                yield TextDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    text=structured_text,
                                )
                                sequence += 1
                        yield UsageDelta(
                            **self._event_base(sequence, request_id=request_id),
                            usage=usage,
                        )
                        sequence += 1
                        yield Done(
                            **self._event_base(sequence, request_id=request_id),
                            finish_reason=_optional_string(completed.get("status")),
                        )
                        return
                    elif event_type in {"response.failed", "response.incomplete"}:
                        yield ProviderError(
                            **self._event_base(sequence),
                            code="provider_response_error",
                            safe_message="provider stream did not complete",
                            retryable=event_type == "response.failed",
                        )
                        return
                yield ProviderError(
                    **self._event_base(sequence),
                    code="incomplete_stream",
                    safe_message="provider stream ended without a terminal event",
                    retryable=True,
                )
        except asyncio.CancelledError:
            raise
        except ProviderException as exc:
            yield ProviderError(
                **self._event_base(sequence, request_id=exc.provider_request_id),
                code=exc.code,
                safe_message=exc.safe_message,
                retryable=exc.retryable,
            )
        except httpx.TimeoutException:
            yield ProviderError(
                **self._event_base(sequence),
                code=ProviderTimeoutError.code,
                safe_message="provider request timed out",
                retryable=True,
            )
        except httpx.HTTPError:
            yield ProviderError(
                **self._event_base(sequence),
                code=ProviderUnavailable.code,
                safe_message="provider request failed",
                retryable=True,
            )

    async def _chat_completions_stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        options: ChatOptions,
    ) -> AsyncIterator[StreamEvent]:
        sequence = 0
        request_id: str | None = None
        finish_reason: str | None = None
        usage: Usage | None = None
        call_ids: dict[str, str] = {}
        call_names: dict[str, str] = {}
        text_parts: list[str] = []
        arguments: dict[str, list[str]] = {}
        try:
            payload = self._request_payload(messages, tools, options, stream=True)
            request_kwargs: dict[str, Any] = {}
            if options.timeout_seconds is not None:
                request_kwargs["timeout"] = options.timeout_seconds
            async with self._client.stream(
                "POST",
                self._endpoint(),
                json=payload,
                headers=self._headers(),
                **request_kwargs,
            ) as response:
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        if usage is None:
                            raise ProviderResponseError(
                                "provider response usage is missing or invalid",
                                retryable=False,
                                provider_request_id=request_id,
                            )
                        if options.response_schema is not None:
                            try:
                                structured_text = self._validated_stream_text(
                                    options.response_schema,
                                    text_parts,
                                    arguments,
                                    call_names,
                                )
                            except StructuredOutputError as exc:
                                yield UsageDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    usage=usage,
                                )
                                sequence += 1
                                yield ProviderError(
                                    **self._event_base(sequence, request_id=request_id),
                                    code=exc.code,
                                    safe_message=exc.safe_message,
                                    retryable=exc.retryable,
                                )
                                return
                            if structured_text is not None:
                                yield TextDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    text=structured_text,
                                )
                                sequence += 1
                        yield UsageDelta(
                            **self._event_base(sequence, request_id=request_id),
                            usage=usage,
                        )
                        sequence += 1
                        yield Done(
                            **self._event_base(sequence, request_id=request_id),
                            finish_reason=finish_reason,
                        )
                        return
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(
                            "provider stream returned invalid JSON", retryable=False
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    request_id = _optional_string(event.get("id")) or request_id
                    if event.get("usage") is not None:
                        usage = _parse_chat_usage(event.get("usage"), request_id=request_id)
                    choices = event.get("choices", [])
                    if not isinstance(choices, list):
                        raise ProviderResponseError(
                            "provider stream choices are invalid", retryable=False
                        )
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise ProviderResponseError(
                                "provider stream choice is invalid", retryable=False
                            )
                        finish_reason = (
                            _optional_string(choice.get("finish_reason")) or finish_reason
                        )
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str):
                            yield ReasoningDelta(
                                **self._event_base(sequence, request_id=request_id),
                                text=reasoning,
                            )
                            sequence += 1
                        visible = delta.get("content")
                        if isinstance(visible, str):
                            if options.response_schema is None:
                                yield TextDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    text=visible,
                                )
                                sequence += 1
                            else:
                                text_parts.append(visible)
                        delta_calls = delta.get("tool_calls", [])
                        if not isinstance(delta_calls, list):
                            raise ProviderResponseError(
                                "provider stream tool calls are invalid", retryable=False
                            )
                        for call in delta_calls:
                            if not isinstance(call, dict):
                                raise ProviderResponseError(
                                    "provider stream tool call is invalid", retryable=False
                                )
                            index = call.get("index")
                            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                                raise ProviderResponseError(
                                    "provider stream tool call index is invalid", retryable=False
                                )
                            item_id = str(index)
                            call_id = call.get("id")
                            if isinstance(call_id, str):
                                call_ids[item_id] = call_id
                            function = call.get("function")
                            if not isinstance(function, dict):
                                continue
                            name = function.get("name")
                            if isinstance(name, str):
                                call_names[item_id] = name
                            argument_delta = function.get("arguments")
                            if not isinstance(argument_delta, str) or not argument_delta:
                                continue
                            if options.response_schema is None:
                                yield ToolCallDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    tool_call_id=call_ids.get(item_id, item_id),
                                    arguments_delta=argument_delta,
                                )
                                sequence += 1
                            else:
                                arguments.setdefault(item_id, []).append(argument_delta)
                raise ProviderResponseError(
                    "provider stream ended without a terminal event",
                    retryable=True,
                    provider_request_id=request_id,
                )
        except asyncio.CancelledError:
            raise
        except ProviderException as exc:
            yield ProviderError(
                **self._event_base(sequence, request_id=exc.provider_request_id or request_id),
                code=exc.code,
                safe_message=exc.safe_message,
                retryable=exc.retryable,
            )
        except httpx.TimeoutException:
            yield ProviderError(
                **self._event_base(sequence, request_id=request_id),
                code=ProviderTimeoutError.code,
                safe_message="provider request timed out",
                retryable=True,
            )
        except httpx.HTTPError:
            yield ProviderError(
                **self._event_base(sequence, request_id=request_id),
                code=ProviderUnavailable.code,
                safe_message="provider request failed",
                retryable=True,
            )

    def _validated_stream_text(
        self,
        response_schema: ResponseSchema,
        text_parts: list[str],
        arguments: dict[str, list[str]],
        item_names: dict[str, str],
    ) -> str | None:
        mode = self._profile.structured_output_mode
        if mode == "native_json_schema":
            unknown = set(arguments).difference(item_names)
            if unknown or (item_names and text_parts):
                raise StructuredOutputError(
                    "structured response is mixed with tool calls",
                    retryable=False,
                )
            if item_names:
                return None
            if not text_parts:
                raise StructuredOutputError(
                    "structured response is missing",
                    retryable=False,
                )
            text = "".join(text_parts)
            parse_structured_text(text, response_schema)
            return text
        if mode == "synthetic_tool":
            reserved = [
                item_id for item_id, name in item_names.items() if name == RESERVED_RESPONSE_TOOL
            ]
            business = [
                item_id for item_id, name in item_names.items() if name != RESERVED_RESPONSE_TOOL
            ]
            unknown = set(arguments).difference(item_names)
            if unknown or (reserved and business):
                raise StructuredOutputError(
                    "structured response must contain one reserved submission only",
                    retryable=False,
                )
            if business:
                if text_parts:
                    raise StructuredOutputError(
                        "structured response is mixed with tool calls",
                        retryable=False,
                    )
                return None
            if len(reserved) != 1:
                raise StructuredOutputError(
                    "structured response is missing",
                    retryable=False,
                )
            payload = "".join(arguments.get(reserved[0], []))
            structured = parse_structured_text(payload, response_schema)
            return json.dumps(
                structured,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        raise UnsupportedCapabilityError(
            "structured output mode is unsupported",
            retryable=False,
        )

    def _request_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        options: ChatOptions,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        if self._profile.api_dialect not in {"responses", "chat_completions"}:
            raise UnsupportedCapabilityError(
                "configured API dialect is not implemented", retryable=False
            )
        if self._profile.api_key is None:
            raise AuthenticationError("provider credential is not configured", retryable=False)
        if any(tool.name == RESERVED_RESPONSE_TOOL for tool in tools):
            raise InvalidRequestError("tool name is reserved", retryable=False)
        if self._profile.api_dialect == "chat_completions":
            return self._chat_completions_payload(messages, tools, options, stream=stream)

        payload: dict[str, Any] = {
            "model": self._profile.model,
            "input": _map_messages(messages),
        }
        mapped_tools = [_map_tool(tool) for tool in tools]
        response_schema = options.response_schema
        if response_schema is not None:
            if any(tool.name == response_schema.name for tool in tools):
                raise InvalidRequestError(
                    "response schema name conflicts with a business tool", retryable=False
                )
            mode = self._profile.structured_output_mode
            if mode == "unsupported":
                raise UnsupportedCapabilityError(
                    "structured output is unsupported by this profile", retryable=False
                )
            if mode == "native_json_schema":
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": response_schema.name,
                        "schema": response_schema.json_schema,
                    }
                }
            elif mode == "synthetic_tool":
                mapped_tools.append(
                    {
                        "type": "function",
                        "name": RESERVED_RESPONSE_TOOL,
                        "description": "Submit the final structured response.",
                        "parameters": response_schema.json_schema,
                    }
                )
            else:
                raise UnsupportedCapabilityError(
                    "structured output mode is unsupported", retryable=False
                )
        if mapped_tools:
            payload["tools"] = mapped_tools
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None:
            payload["max_output_tokens"] = options.max_output_tokens
        if options.tool_choice is not None:
            payload["tool_choice"] = options.tool_choice
        if options.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = options.parallel_tool_calls
        if stream:
            payload["stream"] = True
        return payload

    def _chat_completions_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        options: ChatOptions,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._profile.model,
            "messages": _map_chat_messages(messages),
        }
        mapped_tools = [_map_chat_tool(tool) for tool in tools]
        response_schema = options.response_schema
        if response_schema is not None:
            if any(tool.name == response_schema.name for tool in tools):
                raise InvalidRequestError(
                    "response schema name conflicts with a business tool", retryable=False
                )
            mode = self._profile.structured_output_mode
            if mode == "unsupported":
                raise UnsupportedCapabilityError(
                    "structured output is unsupported by this profile", retryable=False
                )
            if mode == "native_json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema.name,
                        "schema": response_schema.json_schema,
                        "strict": response_schema.strict,
                    },
                }
            elif mode == "synthetic_tool":
                mapped_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": RESERVED_RESPONSE_TOOL,
                            "description": "Submit the final structured response.",
                            "parameters": response_schema.json_schema,
                        },
                    }
                )
            else:
                raise UnsupportedCapabilityError(
                    "structured output mode is unsupported", retryable=False
                )
        if mapped_tools:
            payload["tools"] = mapped_tools
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None:
            payload["max_tokens"] = options.max_output_tokens
        if options.tool_choice is not None:
            payload["tool_choice"] = options.tool_choice
        if options.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = options.parallel_tool_calls
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _parse_response(
        self, body: dict[str, Any], options: ChatOptions, *, usage: Usage
    ) -> ChatResult:
        content: list[Any] = []
        tool_calls: list[ToolCall] = []
        output_text: list[str] = []
        reserved_payloads: list[str] = []
        refusal: str | None = None
        output = body.get("output", [])
        if not isinstance(output, list):
            raise ProviderResponseError("provider output is invalid", retryable=False)
        for item in output:
            if not isinstance(item, dict):
                raise ProviderResponseError("provider output item is invalid", retryable=False)
            item_type = item.get("type")
            if item_type == "message":
                blocks = item.get("content", [])
                if not isinstance(blocks, list):
                    raise ProviderResponseError(
                        "provider message content is invalid", retryable=False
                    )
                for block in blocks:
                    if not isinstance(block, dict):
                        raise ProviderResponseError(
                            "provider message block is invalid", retryable=False
                        )
                    block_type = block.get("type")
                    if block_type == "output_text" and isinstance(block.get("text"), str):
                        text = cast(str, block["text"])
                        output_text.append(text)
                        content.append(TextBlock(text=text))
                    elif block_type == "refusal" and isinstance(block.get("refusal"), str):
                        refusal = cast(str, block["refusal"])
                        content.append(RefusalBlock(reason=refusal))
                    else:
                        content.append(
                            ProviderExtensionBlock(
                                raw_type=str(block_type),
                                raw_payload=cast(dict[str, JsonValue], block),
                            )
                        )
            elif item_type == "function_call":
                name = item.get("name")
                call_id = item.get("call_id", item.get("id"))
                arguments = item.get("arguments")
                if not all(isinstance(value, str) for value in (name, call_id, arguments)):
                    raise ProviderResponseError(
                        "provider function call is invalid", retryable=False
                    )
                if name == RESERVED_RESPONSE_TOOL:
                    reserved_payloads.append(cast(str, arguments))
                    continue
                args = _parse_arguments(cast(str, arguments))
                call = ToolCall(id=cast(str, call_id), name=cast(str, name), args=args)
                tool_calls.append(call)
                content.append(ToolCallBlock(id=call.id, name=call.name, args=call.args))
            elif item_type == "reasoning":
                continue
            else:
                content.append(
                    ProviderExtensionBlock(
                        raw_type=str(item_type), raw_payload=cast(dict[str, JsonValue], item)
                    )
                )

        structured: dict[str, JsonValue] | None = None
        response_schema = options.response_schema
        if response_schema is not None:
            mode = self._profile.structured_output_mode
            if mode == "native_json_schema":
                if tool_calls:
                    if reserved_payloads or output_text:
                        raise StructuredOutputError(
                            "structured response is mixed with business tool calls",
                            retryable=False,
                        )
                elif reserved_payloads or not output_text:
                    raise StructuredOutputError("structured response is missing", retryable=False)
                else:
                    structured = parse_structured_text("".join(output_text), response_schema)
                    content = []
            elif mode == "synthetic_tool":
                if reserved_payloads and tool_calls:
                    raise StructuredOutputError(
                        "structured response is mixed with business tool calls", retryable=False
                    )
                if not reserved_payloads and tool_calls:
                    pass
                elif len(reserved_payloads) != 1:
                    raise StructuredOutputError(
                        "structured response must contain one reserved submission", retryable=False
                    )
                else:
                    structured = parse_structured_text(reserved_payloads[0], response_schema)
                    content = [block for block in content if not isinstance(block, TextBlock)]

        return ChatResult(
            content=tuple(content),
            tool_calls=tuple(tool_calls),
            structured_output=structured,
            usage=usage,
            finish_reason=_optional_string(body.get("status")),
            request_id=_optional_string(body.get("id")),
            refusal=refusal,
            raw_response=_diagnostic_payload(body),
        )

    def _parse_chat_completion(
        self, body: dict[str, Any], options: ChatOptions, *, usage: Usage
    ) -> ChatResult:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderResponseError("provider choices are missing or invalid", retryable=False)
        choice = cast(dict[str, Any], choices[0])
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError("provider message is missing or invalid", retryable=False)

        content: list[Any] = []
        tool_calls: list[ToolCall] = []
        reserved_payloads: list[str] = []
        visible = message.get("content")
        output_text = visible if isinstance(visible, str) else None
        if visible is not None and output_text is None:
            raise ProviderResponseError("provider message content is invalid", retryable=False)
        if output_text is not None:
            content.append(TextBlock(text=output_text))

        refusal = _optional_string(message.get("refusal"))
        if refusal is not None:
            content.append(RefusalBlock(reason=refusal))

        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ProviderResponseError("provider tool calls are invalid", retryable=False)
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise ProviderResponseError("provider tool call is invalid", retryable=False)
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, dict):
                raise ProviderResponseError("provider tool call is invalid", retryable=False)
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                raise ProviderResponseError("provider tool call is invalid", retryable=False)
            if name == RESERVED_RESPONSE_TOOL:
                reserved_payloads.append(arguments)
                continue
            args = _parse_arguments(arguments)
            call = ToolCall(id=call_id, name=name, args=args)
            tool_calls.append(call)
            content.append(ToolCallBlock(id=call.id, name=call.name, args=call.args))

        structured: dict[str, JsonValue] | None = None
        response_schema = options.response_schema
        if response_schema is None:
            if reserved_payloads:
                raise StructuredOutputError(
                    "provider returned an unexpected reserved submission", retryable=False
                )
        elif self._profile.structured_output_mode == "native_json_schema":
            if tool_calls:
                if reserved_payloads or output_text:
                    raise StructuredOutputError(
                        "structured response is mixed with business tool calls", retryable=False
                    )
            elif reserved_payloads or output_text is None:
                raise StructuredOutputError("structured response is missing", retryable=False)
            else:
                structured = parse_structured_text(output_text, response_schema)
                content = [block for block in content if not isinstance(block, TextBlock)]
        elif self._profile.structured_output_mode == "synthetic_tool":
            if reserved_payloads and tool_calls:
                raise StructuredOutputError(
                    "structured response is mixed with business tool calls", retryable=False
                )
            if not reserved_payloads and tool_calls:
                pass
            elif len(reserved_payloads) != 1:
                raise StructuredOutputError(
                    "structured response must contain one reserved submission", retryable=False
                )
            else:
                structured = parse_structured_text(reserved_payloads[0], response_schema)
                content = [block for block in content if not isinstance(block, TextBlock)]
        else:
            raise UnsupportedCapabilityError(
                "structured output mode is unsupported", retryable=False
            )

        return ChatResult(
            content=tuple(content),
            tool_calls=tuple(tool_calls),
            structured_output=structured,
            usage=usage,
            finish_reason=_optional_string(choice.get("finish_reason")),
            request_id=_optional_string(body.get("id")),
            refusal=refusal,
            raw_response=_diagnostic_payload(body),
        )

    def _endpoint(self) -> str:
        if self._profile.api_dialect == "responses":
            return "/responses"
        if self._profile.api_dialect == "chat_completions":
            return "/chat/completions"
        raise UnsupportedCapabilityError(
            "configured API dialect is not implemented", retryable=False
        )

    def _parse_usage(self, value: object, *, request_id: str | None) -> Usage:
        if self._profile.api_dialect == "responses":
            return _parse_usage(value, request_id=request_id)
        if self._profile.api_dialect == "chat_completions":
            return _parse_chat_usage(value, request_id=request_id)
        raise UnsupportedCapabilityError(
            "configured API dialect is not implemented", retryable=False
        )

    def _headers(self) -> dict[str, str]:
        if self._profile.api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._profile.api_key.get_secret_value()}"}

    def _event_base(self, sequence: int, *, request_id: str | None = None) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "provider": self._profile.provider,
            "model": self._profile.model,
            "request_id": request_id,
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        request_id = response.headers.get("x-request-id")
        status = response.status_code
        if status == 401:
            raise AuthenticationError(
                "provider authentication failed",
                retryable=False,
                provider_request_id=request_id,
            )
        if status in {402, 429}:
            raise RateLimitError(
                "provider rate limit or quota was exceeded",
                retryable=status == 429,
                retry_after=_retry_after(response.headers.get("retry-after")),
                provider_request_id=request_id,
            )
        if status == 413:
            raise ContextLengthError(
                "provider context limit was exceeded",
                retryable=False,
                provider_request_id=request_id,
            )
        if status in {400, 404, 422}:
            raise InvalidRequestError(
                "provider rejected the request",
                retryable=False,
                provider_request_id=request_id,
            )
        if status in {500, 502, 503, 504}:
            raise ProviderUnavailable(
                "provider is temporarily unavailable",
                retryable=True,
                provider_request_id=request_id,
            )
        raise ProviderResponseError(
            "provider returned an unexpected status",
            retryable=False,
            provider_request_id=request_id,
        )


def _map_chat_messages(messages: list[Message]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            if not message.tool_call_id:
                raise InvalidRequestError("tool message requires tool_call_id", retryable=False)
            content = message.content if isinstance(message.content, str) else _blocks_text(message)
            mapped.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": content,
                }
            )
            continue
        if isinstance(message.content, str):
            mapped.append({"role": message.role, "content": message.content})
            continue
        tool_blocks = [block for block in message.content if isinstance(block, ToolCallBlock)]
        if tool_blocks and message.role != "assistant":
            raise InvalidRequestError(
                "tool call blocks require an assistant message", retryable=False
            )
        text = _blocks_text(message)
        item: dict[str, Any] = {"role": message.role, "content": text or None}
        if tool_blocks:
            item["tool_calls"] = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(
                            block.args,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for block in tool_blocks
            ]
        mapped.append(item)
    return mapped


def _map_messages(messages: list[Message]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            if not message.tool_call_id:
                raise InvalidRequestError("tool message requires tool_call_id", retryable=False)
            content = message.content if isinstance(message.content, str) else _blocks_text(message)
            mapped.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": content,
                }
            )
            continue
        if isinstance(message.content, str):
            mapped.append({"role": message.role, "content": message.content})
            continue
        text = _blocks_text(message)
        if text:
            mapped.append({"role": message.role, "content": text})
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                mapped.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(
                            block.args,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
    return mapped


def _blocks_text(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
    return "".join(parts)


def _map_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.json_schema,
    }


def _map_chat_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.json_schema,
        },
    }


def _parse_arguments(arguments: str) -> dict[str, JsonValue]:
    try:
        value: Any = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(
            "provider tool arguments are invalid JSON", retryable=False
        ) from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProviderResponseError("provider tool arguments must be an object", retryable=False)
    return cast(dict[str, JsonValue], value)


def _parse_usage(value: object, *, request_id: str | None = None) -> Usage:
    if not isinstance(value, dict):
        raise ProviderResponseError(
            "provider response usage is missing or invalid",
            retryable=False,
            provider_request_id=request_id,
        )
    usage = value
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return Usage(
        input_tokens=_required_nonnegative_int(usage.get("input_tokens"), request_id=request_id),
        output_tokens=_required_nonnegative_int(usage.get("output_tokens"), request_id=request_id),
        cache_read_tokens=_optional_nested_nonnegative_int(
            input_details, "cached_tokens", request_id=request_id
        ),
        reasoning_tokens=_optional_nested_nonnegative_int(
            output_details, "reasoning_tokens", request_id=request_id
        ),
    )


def _parse_chat_usage(value: object, *, request_id: str | None = None) -> Usage:
    if not isinstance(value, dict):
        raise ProviderResponseError(
            "provider response usage is missing or invalid",
            retryable=False,
            provider_request_id=request_id,
        )
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    return Usage(
        input_tokens=_required_nonnegative_int(value.get("prompt_tokens"), request_id=request_id),
        output_tokens=_required_nonnegative_int(
            value.get("completion_tokens"), request_id=request_id
        ),
        cache_read_tokens=_optional_nested_nonnegative_int(
            prompt_details, "cached_tokens", request_id=request_id
        ),
        reasoning_tokens=_optional_nested_nonnegative_int(
            completion_details, "reasoning_tokens", request_id=request_id
        ),
    )


def _optional_nested_nonnegative_int(
    value: object, key: str, *, request_id: str | None
) -> int | None:
    if not isinstance(value, dict) or key not in value:
        return None
    return _required_nonnegative_int(value.get(key), request_id=request_id)


def _required_nonnegative_int(value: object, *, request_id: str | None) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ProviderResponseError(
        "provider response usage is missing or invalid",
        retryable=False,
        provider_request_id=request_id,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _diagnostic_payload(body: dict[str, Any]) -> dict[str, JsonValue]:
    secret_fragments = ("apikey", "authorization", "credential", "password", "secret", "token")

    def sensitive_key(key: object) -> bool:
        normalized = "".join(character for character in str(key).casefold() if character.isalnum())
        return any(fragment in normalized for fragment in secret_fragments)

    def scrub(value: object) -> JsonValue:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if sensitive_key(key) else scrub(child)
                for key, child in value.items()
                if "reasoning" not in str(key).lower()
                and not (key == "type" and child == "reasoning")
            }
        if isinstance(value, list):
            return [
                scrub(child)
                for child in value
                if not (isinstance(child, dict) and child.get("type") == "reasoning")
            ]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    scrubbed = cast(dict[str, JsonValue], scrub(body))
    encoded = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > 64 * 1024:
        return {
            "id": _optional_string(body.get("id")),
            "status": _optional_string(body.get("status")),
            "truncated": True,
        }
    return scrubbed
