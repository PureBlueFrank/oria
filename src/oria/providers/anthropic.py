"""Anthropic Messages adapter normalized into Oria provider contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

import httpx
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validators

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
from oria.providers.structured import RESERVED_RESPONSE_TOOL, validate_structured_value

if TYPE_CHECKING:
    from oria.core.context import Context

_DEFAULT_MAX_TOKENS = 1024


class AnthropicProvider:
    """Normalize one resolved Anthropic Messages profile into Oria contracts."""

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
        selected_tools = tools or []
        payload = self._request_payload(messages, selected_tools, selected_options, stream=False)
        request_kwargs: dict[str, Any] = {}
        if selected_options.timeout_seconds is not None:
            request_kwargs["timeout"] = selected_options.timeout_seconds
        try:
            response = await self._client.post(
                "/v1/messages",
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
        usage = _parse_usage(typed_body.get("usage"), request_id=request_id)
        try:
            return self._parse_response(
                typed_body,
                selected_tools,
                selected_options,
                usage=usage,
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
        selected_tools = tools or []
        sequence = 0
        request_id: str | None = None
        finish_reason: str | None = None
        usage_values: dict[str, object] = {}
        text_parts: list[str] = []
        call_ids: dict[str, str] = {}
        call_names: dict[str, str] = {}
        arguments: dict[str, list[str]] = {}
        try:
            payload = self._request_payload(messages, selected_tools, selected_options, stream=True)
            request_kwargs: dict[str, Any] = {}
            if selected_options.timeout_seconds is not None:
                request_kwargs["timeout"] = selected_options.timeout_seconds
            async with self._client.stream(
                "POST",
                "/v1/messages",
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
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(
                            "provider stream returned invalid JSON", retryable=False
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = event.get("message")
                        if not isinstance(message, dict):
                            raise ProviderResponseError(
                                "provider stream message start is invalid", retryable=False
                            )
                        request_id = _optional_string(message.get("id")) or request_id
                        _merge_usage(usage_values, message.get("usage"))
                    elif event_type == "content_block_start":
                        index = _stream_index(event)
                        block = event.get("content_block")
                        if not isinstance(block, dict):
                            raise ProviderResponseError(
                                "provider stream content block is invalid", retryable=False
                            )
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            call_id = block.get("id")
                            name = block.get("name")
                            if not isinstance(call_id, str) or not isinstance(name, str):
                                raise ProviderResponseError(
                                    "provider stream tool block is invalid", retryable=False
                                )
                            call_ids[index] = call_id
                            call_names[index] = name
                            initial_input = block.get("input")
                            if isinstance(initial_input, dict) and initial_input:
                                arguments.setdefault(index, []).append(
                                    json.dumps(
                                        initial_input,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                )
                        elif block_type == "text" and isinstance(block.get("text"), str):
                            text = cast(str, block["text"])
                            if text:
                                if selected_options.response_schema is None:
                                    yield TextDelta(
                                        **self._event_base(sequence, request_id=request_id),
                                        text=text,
                                    )
                                    sequence += 1
                                else:
                                    text_parts.append(text)
                        elif block_type in {"thinking", "reasoning"}:
                            reasoning = block.get("thinking", block.get("text"))
                            if isinstance(reasoning, str) and reasoning:
                                yield ReasoningDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    text=reasoning,
                                )
                                sequence += 1
                    elif event_type == "content_block_delta":
                        index = _stream_index(event)
                        delta = event.get("delta")
                        if not isinstance(delta, dict):
                            raise ProviderResponseError(
                                "provider stream delta is invalid", retryable=False
                            )
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            delta_text = delta.get("text")
                            if isinstance(delta_text, str):
                                if selected_options.response_schema is None:
                                    yield TextDelta(
                                        **self._event_base(sequence, request_id=request_id),
                                        text=delta_text,
                                    )
                                    sequence += 1
                                else:
                                    text_parts.append(delta_text)
                        elif delta_type == "input_json_delta":
                            partial = delta.get("partial_json")
                            if not isinstance(partial, str):
                                raise ProviderResponseError(
                                    "provider stream tool arguments are invalid", retryable=False
                                )
                            arguments.setdefault(index, []).append(partial)
                            name = call_names.get(index)
                            if (
                                selected_options.response_schema is None
                                and name != RESERVED_RESPONSE_TOOL
                            ):
                                yield ToolCallDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    tool_call_id=call_ids.get(index, index),
                                    arguments_delta=partial,
                                )
                                sequence += 1
                        elif delta_type in {"thinking_delta", "reasoning_delta"}:
                            reasoning = delta.get("thinking", delta.get("text"))
                            if isinstance(reasoning, str):
                                yield ReasoningDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    text=reasoning,
                                )
                                sequence += 1
                    elif event_type == "message_delta":
                        delta = event.get("delta")
                        if isinstance(delta, dict):
                            finish_reason = (
                                _optional_string(delta.get("stop_reason")) or finish_reason
                            )
                        _merge_usage(usage_values, event.get("usage"))
                    elif event_type == "message_stop":
                        usage = _parse_usage(usage_values, request_id=request_id)
                        try:
                            business_arguments = _validate_stream_tool_arguments(
                                arguments,
                                call_names,
                                selected_tools,
                            )
                            structured_text = self._validated_stream_text(
                                selected_options.response_schema,
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
                        if selected_options.response_schema is not None:
                            for index, payload_text in business_arguments.items():
                                yield ToolCallDelta(
                                    **self._event_base(sequence, request_id=request_id),
                                    tool_call_id=call_ids.get(index, index),
                                    arguments_delta=payload_text,
                                )
                                sequence += 1
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
                    elif event_type == "error":
                        raise _stream_error(event, request_id=request_id)
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

    def _request_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        options: ChatOptions,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        if self._profile.api_dialect != "anthropic_messages":
            raise UnsupportedCapabilityError(
                "configured API dialect is not implemented", retryable=False
            )
        if self._profile.api_key is None:
            raise AuthenticationError("provider credential is not configured", retryable=False)
        if any(tool.name == RESERVED_RESPONSE_TOOL for tool in tools):
            raise InvalidRequestError("tool name is reserved", retryable=False)

        system, mapped_messages = _map_messages(messages)
        payload: dict[str, Any] = {
            "model": self._profile.model,
            "messages": mapped_messages,
            "max_tokens": options.max_output_tokens or _DEFAULT_MAX_TOKENS,
        }
        if system:
            payload["system"] = system
        mapped_tools = [_map_tool(tool) for tool in tools]
        response_schema = options.response_schema
        if response_schema is not None:
            if any(tool.name == response_schema.name for tool in tools):
                raise InvalidRequestError(
                    "response schema name conflicts with a business tool", retryable=False
                )
            mode = self._profile.structured_output_mode
            if mode == "native_json_schema":
                payload["output_config"] = {
                    "format": {
                        "type": "json_schema",
                        "schema": response_schema.json_schema,
                    }
                }
            elif mode == "synthetic_tool":
                mapped_tools.append(
                    {
                        "name": RESERVED_RESPONSE_TOOL,
                        "description": "Submit the final structured response.",
                        "input_schema": response_schema.json_schema,
                        "strict": response_schema.strict,
                    }
                )
            elif mode == "unsupported":
                raise UnsupportedCapabilityError(
                    "structured output is unsupported by this profile", retryable=False
                )
            else:
                raise UnsupportedCapabilityError(
                    "structured output mode is unsupported", retryable=False
                )
        if mapped_tools:
            payload["tools"] = mapped_tools
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.tool_choice is not None:
            payload["tool_choice"] = _map_tool_choice(options.tool_choice)
        if options.parallel_tool_calls is not None:
            tool_choice = payload.setdefault("tool_choice", {"type": "auto"})
            if not isinstance(tool_choice, dict):
                raise InvalidRequestError("tool choice is invalid", retryable=False)
            tool_choice["disable_parallel_tool_use"] = not options.parallel_tool_calls
        if stream:
            payload["stream"] = True
        return payload

    def _parse_response(
        self,
        body: dict[str, Any],
        tools: list[ToolSpec],
        options: ChatOptions,
        *,
        usage: Usage,
    ) -> ChatResult:
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ProviderResponseError("provider content is missing or invalid", retryable=False)
        content: list[Any] = []
        tool_calls: list[ToolCall] = []
        output_text: list[str] = []
        reserved_payloads: list[object] = []
        tool_specs = {tool.name: tool for tool in tools}
        for block in blocks:
            if not isinstance(block, dict):
                raise ProviderResponseError("provider content block is invalid", retryable=False)
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ProviderResponseError("provider text block is invalid", retryable=False)
                output_text.append(text)
                content.append(TextBlock(text=text))
            elif block_type == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise ProviderResponseError("provider tool block is invalid", retryable=False)
                if name == RESERVED_RESPONSE_TOOL:
                    reserved_payloads.append(tool_input)
                    continue
                args = _validate_tool_input(tool_input, tool_specs.get(name))
                call = ToolCall(id=call_id, name=name, args=args)
                tool_calls.append(call)
                content.append(ToolCallBlock(id=call.id, name=call.name, args=call.args))
            elif block_type in {"thinking", "reasoning", "redacted_thinking"}:
                continue
            else:
                content.append(
                    ProviderExtensionBlock(
                        raw_type=str(block_type),
                        raw_payload=cast(dict[str, JsonValue], block),
                    )
                )

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
            elif reserved_payloads or not output_text:
                raise StructuredOutputError("structured response is missing", retryable=False)
            else:
                structured = _parse_structured_text("".join(output_text), response_schema)
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
                structured = validate_structured_value(reserved_payloads[0], response_schema)
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
            finish_reason=_optional_string(body.get("stop_reason")),
            request_id=_optional_string(body.get("id")),
            refusal=None,
            raw_response=_diagnostic_payload(body),
        )

    def _validated_stream_text(
        self,
        response_schema: ResponseSchema | None,
        text_parts: list[str],
        arguments: dict[str, list[str]],
        call_names: dict[str, str],
    ) -> str | None:
        if response_schema is None:
            if any(name == RESERVED_RESPONSE_TOOL for name in call_names.values()):
                raise StructuredOutputError(
                    "provider returned an unexpected reserved submission", retryable=False
                )
            return None
        mode = self._profile.structured_output_mode
        if mode == "native_json_schema":
            if call_names:
                if text_parts or any(
                    name == RESERVED_RESPONSE_TOOL for name in call_names.values()
                ):
                    raise StructuredOutputError(
                        "structured response is mixed with business tool calls", retryable=False
                    )
                return None
            if not text_parts:
                raise StructuredOutputError("structured response is missing", retryable=False)
            text = "".join(text_parts)
            _parse_structured_text(text, response_schema)
            return text
        if mode == "synthetic_tool":
            reserved = [
                index for index, name in call_names.items() if name == RESERVED_RESPONSE_TOOL
            ]
            business = [
                index for index, name in call_names.items() if name != RESERVED_RESPONSE_TOOL
            ]
            unknown = set(arguments).difference(call_names)
            if unknown or (reserved and business):
                raise StructuredOutputError(
                    "structured response must contain one reserved submission only",
                    retryable=False,
                )
            if business:
                if text_parts:
                    raise StructuredOutputError(
                        "structured response is mixed with business tool calls", retryable=False
                    )
                return None
            if len(reserved) != 1:
                raise StructuredOutputError("structured response is missing", retryable=False)
            payload = "".join(arguments.get(reserved[0], []))
            structured = _parse_structured_text(payload, response_schema)
            return json.dumps(
                structured,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        raise UnsupportedCapabilityError("structured output mode is unsupported", retryable=False)

    def _headers(self) -> dict[str, str]:
        if self._profile.api_key is None:
            return {"anthropic-version": "2023-06-01"}
        return {
            "x-api-key": self._profile.api_key.get_secret_value(),
            "anthropic-version": "2023-06-01",
        }

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
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
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
        if status in {500, 502, 503, 504, 529}:
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


def _map_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    mapped: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(
                message.content if isinstance(message.content, str) else _blocks_text(message)
            )
            continue
        if message.role == "tool":
            if not message.tool_call_id:
                raise InvalidRequestError("tool message requires tool_call_id", retryable=False)
            content = message.content if isinstance(message.content, str) else _blocks_text(message)
            mapped.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": content,
                        }
                    ],
                }
            )
            continue
        blocks: list[dict[str, Any]] = []
        if isinstance(message.content, str):
            blocks.append({"type": "text", "text": message.content})
        else:
            for block in message.content:
                if isinstance(block, TextBlock):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolCallBlock) and message.role == "assistant":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.args,
                        }
                    )
                elif isinstance(block, ToolResultBlock) and message.role == "user":
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_call_id,
                            "content": block.content,
                        }
                    )
                else:
                    raise InvalidRequestError(
                        "message content block is unsupported by Anthropic Messages",
                        retryable=False,
                    )
        mapped.append({"role": message.role, "content": blocks})
    return "\n\n".join(system_parts), mapped


def _blocks_text(message: Message) -> str:
    return "".join(
        block.text if isinstance(block, TextBlock) else block.content
        for block in message.content
        if isinstance(block, (TextBlock, ToolResultBlock))
    )


def _map_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.json_schema,
        "strict": tool.strict,
    }


def _map_tool_choice(value: str | dict[str, JsonValue]) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    aliases = {"required": "any", "auto": "auto", "none": "none"}
    return {"type": aliases.get(value, "tool"), **({"name": value} if value not in aliases else {})}


def _parse_structured_text(text: str, response_schema: ResponseSchema) -> dict[str, JsonValue]:
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            "structured response is not valid JSON", retryable=False
        ) from exc
    return validate_structured_value(value, response_schema)


def _validate_tool_input(value: object, tool: ToolSpec | None) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProviderResponseError("provider tool input must be an object", retryable=False)
    if tool is not None:
        try:
            validators.validator_for(tool.json_schema)(tool.json_schema).validate(value)
        except JsonSchemaValidationError as exc:
            raise ProviderResponseError(
                "provider tool input does not match its schema", retryable=False
            ) from exc
    return cast(dict[str, JsonValue], value)


def _validate_stream_tool_arguments(
    arguments: dict[str, list[str]],
    call_names: dict[str, str],
    tools: list[ToolSpec],
) -> dict[str, str]:
    tool_specs = {tool.name: tool for tool in tools}
    business: dict[str, str] = {}
    unknown = set(arguments).difference(call_names)
    if unknown:
        raise ProviderResponseError("provider stream tool call is invalid", retryable=False)
    for index, name in call_names.items():
        payload = "".join(arguments.get(index, []))
        if not payload:
            raise ProviderResponseError(
                "provider stream tool arguments are missing", retryable=False
            )
        try:
            value: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                "provider stream tool arguments are invalid JSON", retryable=False
            ) from exc
        if name == RESERVED_RESPONSE_TOOL:
            continue
        _validate_tool_input(value, tool_specs.get(name))
        business[index] = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return business


def _parse_usage(value: object, *, request_id: str | None = None) -> Usage:
    if not isinstance(value, dict):
        raise ProviderResponseError(
            "provider response usage is missing or invalid",
            retryable=False,
            provider_request_id=request_id,
        )
    return Usage(
        input_tokens=_required_nonnegative_int(value.get("input_tokens"), request_id=request_id),
        output_tokens=_required_nonnegative_int(value.get("output_tokens"), request_id=request_id),
        cache_read_tokens=_optional_nonnegative_int(
            value.get("cache_read_input_tokens"), request_id=request_id
        ),
        cache_write_tokens=_optional_nonnegative_int(
            value.get("cache_creation_input_tokens"), request_id=request_id
        ),
        reasoning_tokens=_optional_nonnegative_int(
            value.get("reasoning_tokens"), request_id=request_id
        ),
    )


def _merge_usage(target: dict[str, object], value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProviderResponseError("provider stream usage is invalid", retryable=False)
    target.update(value)


def _required_nonnegative_int(value: object, *, request_id: str | None) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ProviderResponseError(
        "provider response usage is missing or invalid",
        retryable=False,
        provider_request_id=request_id,
    )


def _optional_nonnegative_int(value: object, *, request_id: str | None) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(value, request_id=request_id)


def _stream_index(event: dict[str, Any]) -> str:
    value = event.get("index")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    raise ProviderResponseError("provider stream content index is invalid", retryable=False)


def _stream_error(event: dict[str, Any], *, request_id: str | None) -> ProviderException:
    error = event.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    if error_type == "authentication_error":
        return AuthenticationError(
            "provider authentication failed",
            retryable=False,
            provider_request_id=request_id,
        )
    if error_type == "rate_limit_error":
        return RateLimitError(
            "provider rate limit or quota was exceeded",
            retryable=True,
            provider_request_id=request_id,
        )
    if error_type in {"overloaded_error", "api_error"}:
        return ProviderUnavailable(
            "provider is temporarily unavailable",
            retryable=True,
            provider_request_id=request_id,
        )
    return ProviderResponseError(
        "provider stream failed",
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
            block_type = value.get("type")
            if block_type in {"thinking", "reasoning", "redacted_thinking"}:
                return None
            return {
                str(key): "[REDACTED]" if sensitive_key(key) else scrub(child)
                for key, child in value.items()
                if "reasoning" not in str(key).lower() and "thinking" not in str(key).lower()
            }
        if isinstance(value, list):
            return [
                scrub(child)
                for child in value
                if not (
                    isinstance(child, dict)
                    and child.get("type") in {"thinking", "reasoning", "redacted_thinking"}
                )
            ]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    scrubbed = cast(dict[str, JsonValue], scrub(body))
    encoded = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > 64 * 1024:
        return {
            "id": _optional_string(body.get("id")),
            "stop_reason": _optional_string(body.get("stop_reason")),
            "truncated": True,
        }
    return scrubbed
