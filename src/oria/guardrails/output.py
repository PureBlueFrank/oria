"""Deterministic direct-identifier, credential, and toxicity redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from oria.core.types import GuardrailResult, JsonValue
from oria.memory.persistent import redact_memory_content

if TYPE_CHECKING:
    from oria.core.context import Context

_TOXICITY = re.compile(
    r"(?i)\b(?:kill\s+yourself|racial\s+slur|hate\s+speech)\b|(?:去死|种族歧视|仇恨言论)"
)
_TOXICITY_REDACTED = "[TOXICITY_REDACTED]"


def redact_output_content(content: Any) -> JsonValue:
    """Return a JSON-safe copy with sensitive strings deterministically redacted."""

    if isinstance(content, str):
        return _TOXICITY.sub(_TOXICITY_REDACTED, redact_memory_content(content))
    if content is None or isinstance(content, (bool, int, float)):
        return cast(JsonValue, content)
    if isinstance(content, Mapping):
        return {
            str(key): redact_output_content(value)
            for key, value in content.items()
        }
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
