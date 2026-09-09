"""Prompt-injection detection used only as defense in depth."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

from oria.core.types import GuardrailResult

if TYPE_CHECKING:
    from oria.core.context import Context

_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b"),
    re.compile(r"(?i)\b(reveal|show|print|leak)\s+(the\s+)?system\s+prompt\b"),
    re.compile(r"(?i)\b(jailbreak|developer\s+mode|do\s+anything\s+now)\b"),
    re.compile(r"忽略(?:之前|以上|所有)(?:的)?指令"),
    re.compile(r"(?:泄露|显示|输出)(?:你的)?系统提示(?:词)?"),
    re.compile(r"(?:越狱|开发者模式|无视安全规则)"),
)


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if hasattr(content, "content"):
        return _text_content(content.content)
    if isinstance(content, dict):
        return " ".join(_text_content(value) for value in content.values())
    if isinstance(content, (list, tuple)):
        return " ".join(_text_content(value) for value in content)
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def contains_prompt_injection(content: Any) -> bool:
    """Return a deterministic signal; never use it as an authorization decision."""

    text = _text_content(content)
    return any(pattern.search(text) is not None for pattern in _INJECTION_PATTERNS)


class PromptInjectionGuardrail:
    phase: Literal["input", "output", "tool"] = "input"

    async def check(self, content: Any, ctx: Context) -> GuardrailResult:
        del ctx
        detected = contains_prompt_injection(content)
        return GuardrailResult(
            passed=not detected,
            reason="prompt_injection_detected" if detected else None,
            action="warn",
        )


class RAGInjectionGuardrail:
    phase: Literal["input", "output", "tool"] = "input"

    async def check(self, content: Any, ctx: Context) -> GuardrailResult:
        del ctx
        detected = contains_prompt_injection(content)
        return GuardrailResult(
            passed=not detected,
            reason="untrusted_rag_injection_detected" if detected else None,
            action="warn",
        )
