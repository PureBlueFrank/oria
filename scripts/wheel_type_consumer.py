"""Minimal downstream typed-package consumer used by the wheel smoke."""

from oria.core.types import JsonValue


def normalize(value: JsonValue) -> JsonValue:
    return value


payload: JsonValue = {"finite": 1.25, "items": [True, None, "ok"]}
normalized = normalize(payload)
