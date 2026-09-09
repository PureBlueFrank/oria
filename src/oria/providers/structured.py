"""Local structured-output validation shared by provider adapters."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validators

from oria.core.types import JsonValue, ResponseSchema
from oria.providers.errors import StructuredOutputError

RESERVED_RESPONSE_TOOL = "__oria_submit_response__"

_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def validate_structured_value(
    value: object, response_schema: ResponseSchema
) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StructuredOutputError("structured response must be a JSON object", retryable=False)
    try:
        schema = (
            _strict_schema(response_schema.json_schema)
            if response_schema.strict
            else response_schema.json_schema
        )
        validator_type = validators.validator_for(schema)
        validator_type(schema).validate(value)
    except JsonSchemaValidationError as exc:
        raise StructuredOutputError(
            "structured response does not match the requested schema", retryable=False
        ) from exc
    return cast(dict[str, JsonValue], value)


def _strict_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    def clone(value: object) -> Any:
        if isinstance(value, dict):
            return {str(key): clone(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [clone(child) for child in value]
        return value

    strict = cast(dict[str, JsonValue], clone(schema))

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and not isinstance(
                node.get("additionalProperties"), dict
            ):
                node["additionalProperties"] = False
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(strict)
    return strict


def _salvage_json_text(text: str) -> str:
    """Recover common but harmless model formatting slips before parsing.

    Only syntactic repairs that cannot change meaning are applied: trimming a surrounding
    Markdown code fence, dropping prose before/after the outermost JSON value, and removing
    trailing commas before a closing bracket/brace. The result still passes through full
    schema validation afterwards.
    """

    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", stripped, re.DOTALL)
    if fence is not None:
        stripped = fence.group(1).strip()
    if not stripped.lstrip().startswith(("{", "[")):
        start = stripped.find("{")
        if start == -1:
            start = stripped.find("[")
        if start == -1:
            return text
        end = _last_closing_brace(stripped, start)
        if end == -1:
            return text
        stripped = stripped[start : end + 1].strip()
    stripped = _TRAILING_COMMA.sub(r"\1", stripped)
    return stripped


def _last_closing_brace(text: str, start: int) -> int:
    """Return the index of the closing delimiter matching the opener at ``start``."""

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def parse_structured_text(text: str, response_schema: ResponseSchema) -> dict[str, JsonValue]:
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        salvaged = _salvage_json_text(text)
        if salvaged != text.strip():
            try:
                value = json.loads(salvaged)
            except json.JSONDecodeError:
                raise StructuredOutputError(
                    "structured response is not valid JSON", retryable=False
                ) from exc
        else:
            raise StructuredOutputError(
                "structured response is not valid JSON", retryable=False
            ) from exc
    return validate_structured_value(value, response_schema)
