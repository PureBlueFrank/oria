"""Local structured-output validation shared by provider adapters."""

from __future__ import annotations

import json
from typing import Any, cast

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validators

from oria.core.types import JsonValue, ResponseSchema
from oria.providers.errors import StructuredOutputError

RESERVED_RESPONSE_TOOL = "__oria_submit_response__"


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
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(strict)
    return strict


def parse_structured_text(text: str, response_schema: ResponseSchema) -> dict[str, JsonValue]:
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            "structured response is not valid JSON", retryable=False
        ) from exc
    return validate_structured_value(value, response_schema)
