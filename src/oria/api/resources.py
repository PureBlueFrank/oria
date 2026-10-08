"""Resource authorization seam shared by HTTP and future worker entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.context import Context
from oria.core.types import AuthorizationContext, AuthorizationRequest, JsonValue, ResourceRef


async def authorize_resource(
    *,
    binding: IdentityBinding,
    directory: Callable[[], IdentityDirectory],
    ctx: Context,
    resource: ResourceRef,
    action: str,
    resource_attributes: Mapping[str, JsonValue],
    requester: str | None = None,
) -> None:
    """Re-resolve identity, then use the existing PolicyEngine, including ABAC.

    Callers load resources using the trusted tenant first and use the same generic
    not-found response for missing and denied resources. Requester comes from the
    stored approval, never from the HTTP body. Resource attributes are explicitly
    supplied by the repository/service, including {} for unscoped resources; they
    must never be copied from client input.
    """
    current = directory().resolve(binding.issuer, binding.subject)
    if current != binding or ctx.actor != current.actor or ctx.executor != current.executor:
        raise AuthenticationError("identity changed; create a fresh execution context")
    if resource.tenant_id != current.actor.tenant_id:
        raise PermissionError("resource unavailable")
    if (
        action.startswith("approval:")
        and action.endswith(":decide")
        and (requester is None or requester == current.actor.subject_id)
    ):
        raise PermissionError("resource unavailable")
    decision = await ctx.policy.authorize(
        AuthorizationRequest(
            actor=current.actor,
            executor=current.executor,
            action=action,
            resource=resource,
            context=AuthorizationContext(
                correlation_id=ctx.correlation_id,
                attributes={"resource_attributes": dict(resource_attributes)},
            ),
        ),
        ctx,
    )
    if not decision.allow:
        raise PermissionError("resource unavailable")
