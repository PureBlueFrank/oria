"""Structured-output text salvage must never weaken schema validation."""

from __future__ import annotations

import pytest

from oria.core.types import ResponseSchema
from oria.providers.errors import StructuredOutputError
from oria.providers.structured import parse_structured_text

pytestmark = pytest.mark.unit

_SCHEMA = ResponseSchema(
    name="salvage_test",
    json_schema={
        "type": "object",
        "properties": {"outcome": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["outcome"],
    },
)


def test_fenced_json_is_salvaged_and_validated() -> None:
    text = '```json\n{"outcome": "attributed", "count": 3}\n```'
    assert parse_structured_text(text, _SCHEMA) == {"outcome": "attributed", "count": 3}


def test_trailing_comma_is_salvaged() -> None:
    text = '{"outcome": "attributed", "count": 3,}'
    assert parse_structured_text(text, _SCHEMA) == {"outcome": "attributed", "count": 3}


def test_trailing_comma_in_array_is_salvaged() -> None:
    schema = ResponseSchema(
        name="array_test",
        json_schema={
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
        },
    )
    text = '{"items": ["a", "b",]}'
    assert parse_structured_text(text, schema) == {"items": ["a", "b"]}


def test_salvage_still_enforces_schema() -> None:
    text = '```json\n{"count": 3}\n```'
    with pytest.raises(StructuredOutputError):
        parse_structured_text(text, _SCHEMA)


def test_unrecoverable_json_raises() -> None:
    with pytest.raises(StructuredOutputError):
        parse_structured_text('{"outcome": "attributed"', _SCHEMA)
