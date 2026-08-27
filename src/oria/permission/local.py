"""Deny-by-default policy for the trusted community-local identity profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from oria.core.types import AuthorizationRequest, PolicyDecision, Principal

if TYPE_CHECKING:
    from oria.core.context import Context

LOCAL_TENANT_ID = "local-community"
LOCAL_USER_SUBJECT_ID = "local-operator"
LOCAL_CLI_SUBJECT_ID = "oria-cli"
LOCAL_POLICY_VERSION = "local-v1"

_LOCAL_READ_ACTIONS = frozenset(
    {
        "campaign:read",
        "config:read",
        "ingress:submit",
        "merchant:read",
        "rule:read",
    }
)


def local_operator() -> Principal:
    """Return the fixed community actor; no caller-supplied roles are accepted."""
    return Principal(
        subject_id=LOCAL_USER_SUBJECT_ID,
        tenant_id=LOCAL_TENANT_ID,
        kind="human",
        roles=("operator",),
        authn_method="trusted-local-profile",
    )


def local_cli_executor() -> Principal:
    """Return the fixed CLI workload identity, separate from the human actor."""
    return Principal(
        subject_id=LOCAL_CLI_SUBJECT_ID,
        tenant_id=LOCAL_TENANT_ID,
        kind="service",
        roles=("runtime",),
        authn_method="trusted-local-profile",
    )


class LocalPolicyEngine:
    """Authorize the narrow local read surface and reject all spoofed identities."""

    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision:
        reason = self._denial_reason(request, ctx)
        return PolicyDecision(
            allow=reason is None,
            constraints={"tenant_id": LOCAL_TENANT_ID} if reason is None else {},
            policy_version=LOCAL_POLICY_VERSION,
            reason="allowed by trusted local profile" if reason is None else reason,
        )

    @staticmethod
    def _denial_reason(request: AuthorizationRequest, ctx: Context) -> str | None:
        if request.actor != ctx.actor or request.executor != ctx.executor:
            return "authorization principals do not match the trusted context"
        if request.actor != local_operator() or request.executor != local_cli_executor():
            return "principal is not the trusted community identity"
        if request.resource.tenant_id != LOCAL_TENANT_ID:
            return "cross-tenant access is denied"
        if request.action not in _LOCAL_READ_ACTIONS:
            return "action is not allowed by the local read policy"
        return None
