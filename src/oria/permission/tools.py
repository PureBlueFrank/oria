"""Policy-backed runtime tool exposure decisions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from oria.core.types import AuthorizationContext, AuthorizationRequest, ResourceRef

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.core.protocols import Tool
    from oria.tools.registry import ToolRegistry


def tool_authorization_request(
    tool: Tool,
    ctx: Context,
) -> AuthorizationRequest:
    """Build the canonical authorization request for one tool capability."""

    return AuthorizationRequest(
        actor=ctx.actor,
        executor=ctx.executor,
        action=tool.policy.required_action,
        resource=ResourceRef(
            resource_type=tool.policy.resource_type,
            resource_id=tool.name,
            tenant_id=ctx.tenant_id,
        ),
        context=AuthorizationContext(
            correlation_id=getattr(ctx, "correlation_id", ctx.run_id),
        ),
    )


async def authorized_tool_names(
    registry: ToolRegistry,
    ctx: Context,
    names: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return the configured tool subset currently allowed by ``ctx.policy``."""

    selected = tuple(registry) if names is None else tuple(names)
    if len(set(selected)) != len(selected):
        raise ValueError("tool selection contains duplicates")
    allowed: list[str] = []
    for name in selected:
        tool = registry.get(name)
        decision = await ctx.policy.authorize(tool_authorization_request(tool, ctx), ctx)
        if decision.allow and decision.constraints.get("tenant_id") == ctx.tenant_id:
            allowed.append(name)
    return tuple(allowed)
