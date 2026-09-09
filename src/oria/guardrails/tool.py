"""Execution-time tool authorization recheck."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from oria.core.types import AuthorizationRequest, GuardrailResult

if TYPE_CHECKING:
    from oria.core.context import Context


class ToolAuthorizationGuardrail:
    phase: Literal["input", "output", "tool"] = "tool"

    async def check(self, content: Any, ctx: Context) -> GuardrailResult:
        if not isinstance(content, AuthorizationRequest):
            return GuardrailResult(
                passed=False,
                reason="invalid_tool_authorization_request",
                action="block",
            )
        decision = await ctx.policy.authorize(content, ctx)
        allowed = decision.allow and decision.constraints.get("tenant_id") == ctx.tenant_id
        return GuardrailResult(
            passed=allowed,
            reason=None if allowed else "tool_authorization_denied",
            action="block",
        )
