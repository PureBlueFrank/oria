"""Deterministic direct-identifier, credential, and toxicity redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from oria.core.types import GuardrailResult, JsonValue

if TYPE_CHECKING:
    from oria.core.context import Context

_TOXICITY = re.compile(
    r"(?i)\b(?:kill\s+yourself|racial\s+slur|hate\s+speech)\b|(?:去死|种族歧视|仇恨言论)"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\d-])(?:\+?86[ -]?)?1[3-9]\d(?:[ -]?\d){8}(?!\d)")
_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer|password|secret|token)\b\s*[:=]\s*\S+"
)
_REDACTED = "[REDACTED]"
_TOXICITY_REDACTED = "[TOXICITY_REDACTED]"


def _redact_text(content: str) -> str:
    redacted = _SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", content)
    redacted = _EMAIL.sub(_REDACTED, redacted)
    redacted = _PHONE.sub(_REDACTED, redacted)
    return _TOXICITY.sub(_TOXICITY_REDACTED, redacted)


def redact_output_content(content: Any) -> JsonValue:
    """Return a JSON-safe copy with sensitive strings deterministically redacted."""

    if isinstance(content, str):
        return _redact_text(content)
    if content is None or isinstance(content, (bool, int, float)):
        return cast(JsonValue, content)
    if isinstance(content, Mapping):
        return {str(key): redact_output_content(value) for key, value in content.items()}
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        return [redact_output_content(value) for value in content]
    if hasattr(content, "model_dump"):
        return redact_output_content(content.model_dump(mode="json"))
    return redact_output_content(str(content))


class OutputSafetyGuardrail:
    phase: Literal["input", "output", "tool"] = "output"

    async def check(self, content: Any, ctx: Context) -> GuardrailResult:
        del ctx
        sanitized = redact_output_content(content)
        changed = sanitized != content
        return GuardrailResult(
            passed=not changed,
            reason="sensitive_output_redacted" if changed else None,
            action="redact",
            sanitized_content=sanitized,
        )
