"""Deterministic offline LLM provider for fixture and contract execution."""

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
    ProviderCapabilities,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolSpec,
    Usage,
    UsageDelta,
)
from oria.providers.errors import StructuredOutputError
from oria.providers.structured import validate_structured_value

if TYPE_CHECKING:
    from oria.core.context import Context


class MockLLMProvider:
    """Return an injected fixed result or a deterministic schema-shaped result."""

    def __init__(self, fixed_result: ChatResult | None = None) -> None:
        self._fixed_result = fixed_result

    async def capabilities(self, ctx: Context) -> ProviderCapabilities:
        del ctx
        return ProviderCapabilities(
            tool_calling=True,
            streaming=True,
            reasoning=False,
            structured_output=True,
            parallel_tool_calls=True,
            structured_output_modes=frozenset({"native_json_schema"}),
            api_dialect="mock",
        )

    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        del ctx, tools
        if self._fixed_result is not None:
            if selected_schema := (options or ChatOptions()).response_schema:
                if self._fixed_result.structured_output is None:
                    raise StructuredOutputError(
                        "fixed mock result is missing structured output", retryable=False
                    )
                validate_structured_value(self._fixed_result.structured_output, selected_schema)
                if self._fixed_result.tool_calls:
                    raise StructuredOutputError(
                        "fixed mock result mixes structured output and tool calls", retryable=False
                    )
            return self._fixed_result
        selected_options = options or ChatOptions()
        usage = Usage(input_tokens=0, output_tokens=0)
        if selected_options.response_schema is not None:
            value = _schema_fixture(selected_options.response_schema.json_schema)
            structured = validate_structured_value(value, selected_options.response_schema)
            return ChatResult(
                content=(),
                tool_calls=(),
                structured_output=structured,
                usage=usage,
                finish_reason="stop",
                request_id="mock-response",
            )
        prompt = _last_text(messages)
        return ChatResult(
            content=(TextBlock(text=prompt),),
            tool_calls=(),
            usage=usage,
            finish_reason="stop",
            request_id="mock-response",
        )

    async def chat_stream(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        result = await self.chat(messages, ctx, tools, options)
        sequence = 0
        for block in result.content:
            if isinstance(block, TextBlock):
                yield TextDelta(
                    sequence=sequence,
                    provider="mock",
                    model="mock-demo",
                    request_id=result.request_id,
                    text=block.text,
                )
                sequence += 1
        for tool_call in result.tool_calls:
            yield ToolCallDelta(
                sequence=sequence,
                provider="mock",
                model="mock-demo",
                request_id=result.request_id,
                tool_call_id=tool_call.id,
                arguments_delta=json.dumps(
                    tool_call.args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
            sequence += 1
        yield UsageDelta(
            sequence=sequence,
            provider="mock",
            model="mock-demo",
            request_id=result.request_id,
            usage=result.usage,
        )
        yield Done(
            sequence=sequence + 1,
            provider="mock",
            model="mock-demo",
            request_id=result.request_id,
            finish_reason=result.finish_reason,
        )


def _last_text(messages: list[Message]) -> str:
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextBlock))


def _schema_fixture(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = _fixture_value(schema, root=schema, resolving=frozenset())
    if not isinstance(value, dict):
        raise StructuredOutputError("mock response schema root is not an object", retryable=False)
    return value


def _object_fixture(
    schema: dict[str, Any],
    *,
    root: dict[str, JsonValue],
    resolving: frozenset[str],
) -> dict[str, JsonValue]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    required = schema.get("required", [])
    names = list(required) if isinstance(required, (list, tuple)) else []
    minimum = schema.get("minProperties", 0)
    if isinstance(minimum, int) and minimum > len(names):
        optional = sorted(name for name in properties if name not in names)
        names.extend(optional[: minimum - len(names)])
    result: dict[str, JsonValue] = {}
    for name in names:
        if isinstance(name, str) and isinstance(properties.get(name), dict):
            result[name] = _fixture_value(
                cast(dict[str, Any], properties[name]),
                root=root,
                resolving=resolving,
            )
    return result


def _fixture_value(
    schema: dict[str, Any],
    *,
    root: dict[str, JsonValue],
    resolving: frozenset[str],
) -> JsonValue:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in resolving:
            raise StructuredOutputError("mock response schema contains a cycle", retryable=False)
        target: object = root
        if not reference.startswith("#/"):
            raise StructuredOutputError(
                "mock supports only local schema references", retryable=False
            )
        for token in reference[2:].split("/"):
            if not isinstance(target, dict):
                raise StructuredOutputError("mock schema reference is invalid", retryable=False)
            target = target.get(token.replace("~1", "/").replace("~0", "~"))
        if not isinstance(target, dict):
            raise StructuredOutputError("mock schema reference is invalid", retryable=False)
        return _fixture_value(
            cast(dict[str, Any], target),
            root=root,
            resolving=resolving | {reference},
        )
    for keyword in ("oneOf", "anyOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return _fixture_value(
                cast(dict[str, Any], choices[0]),
                root=root,
                resolving=resolving,
            )
    if "default" in schema:
        return cast(JsonValue, schema["default"])
    if "const" in schema:
        return cast(JsonValue, schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return cast(JsonValue, enum[0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), kind[0] if kind else None)
    if kind == "object":
        return _object_fixture(schema, root=root, resolving=resolving)
    if kind == "array":
        minimum = schema.get("minItems", 0)
        count = minimum if isinstance(minimum, int) and minimum > 0 else 0
        items = schema.get("items", {})
        if not isinstance(items, dict):
            return []
        return [_fixture_value(items, root=root, resolving=resolving) for _ in range(count)]
    if kind == "integer":
        minimum = schema.get("minimum")
        exclusive = schema.get("exclusiveMinimum")
        if isinstance(exclusive, int) and not isinstance(exclusive, bool):
            return exclusive + 1
        return minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else 0
    if kind == "number":
        minimum = schema.get("minimum")
        exclusive = schema.get("exclusiveMinimum")
        if isinstance(exclusive, (int, float)) and not isinstance(exclusive, bool):
            return float(exclusive) + 1.0
        return float(minimum) if isinstance(minimum, (int, float)) else 0.0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    minimum_length = schema.get("minLength", 0)
    length = minimum_length if isinstance(minimum_length, int) and minimum_length > 0 else 0
    return "x" * length
