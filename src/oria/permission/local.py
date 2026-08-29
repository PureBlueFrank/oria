"""Deny-by-default policy for the trusted community-local identity profile."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from oria.core.protocols import AuditService
from oria.core.types import (
    ACLFilter,
    AuthorizationRequest,
    EventEnvelope,
    PolicyDecision,
    Principal,
)

if TYPE_CHECKING:
    from oria.core.context import Context

LOCAL_TENANT_ID = "local-community"
LOCAL_USER_SUBJECT_ID = "local-operator"
LOCAL_CLI_SUBJECT_ID = "oria-cli"
LOCAL_POLICY_VERSION = "local-v1"

_LOCAL_ACTIONS = frozenset(
    {
        "campaign:read",
        "config:read",
        "document:read",
        "ingress:submit",
        "knowledge:delete",
        "knowledge:write",
        "merchant:read",
        "rule:read",
    }
)
_DOCUMENT_READ_ACTIONS = frozenset({"document:read", "rule:read"})
_DENIAL_REASONS = {
    "missing_principals": "authorization principals are required",
    "context_mismatch": "authorization principals do not match the trusted context",
    "untrusted_principal": "principal is not the trusted community identity",
    "cross_tenant": "cross-tenant access is denied",
    "unknown_action": "action is not allowed by the local read policy",
}


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

    def __init__(self, audit: AuditService | None = None) -> None:
        self._audit = audit

    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision:
        denial_code = self._denial_code(request, ctx)
        allowed = denial_code is None
        acl_filter = None
        if allowed and request.action in _DOCUMENT_READ_ACTIONS:
            acl_filter = ACLFilter(
                tenant_id=LOCAL_TENANT_ID,
                allowed_subject_ids=(request.actor.subject_id,),
                allowed_roles=request.actor.roles,
                classifications=("public", "internal", "restricted"),
            )
        decision = PolicyDecision(
            allow=allowed,
            constraints={"tenant_id": LOCAL_TENANT_ID} if allowed else {},
            policy_version=LOCAL_POLICY_VERSION,
            reason=(
                "allowed by trusted local profile"
                if denial_code is None
                else _DENIAL_REASONS[denial_code]
            ),
            acl_filter=acl_filter,
        )
        if self._audit is not None:
            await self._audit.append(
                self._audit_event(request, ctx, decision, denial_code),
                classification=self._audit_classification(request),
            )
        return decision

    @staticmethod
    def _denial_code(request: AuthorizationRequest, ctx: Context) -> str | None:
        actor = getattr(request, "actor", None)
        executor = getattr(request, "executor", None)
        context_actor = getattr(ctx, "actor", None)
        context_executor = getattr(ctx, "executor", None)
        if actor is None or executor is None or context_actor is None or context_executor is None:
            return "missing_principals"
        if actor != context_actor or executor != context_executor:
            return "context_mismatch"
        if actor != local_operator() or executor != local_cli_executor():
            return "untrusted_principal"
        if request.resource.tenant_id != LOCAL_TENANT_ID:
            return "cross_tenant"
        if request.action not in _LOCAL_ACTIONS:
            return "unknown_action"
        return None

    @staticmethod
    def _audit_classification(request: AuthorizationRequest) -> str:
        value = request.context.attributes.get("classification")
        if isinstance(value, str) and value in {"public", "internal", "restricted"}:
            return value
        if request.action in _DOCUMENT_READ_ACTIONS:
            return "restricted"
        return "internal"

    @staticmethod
    def _audit_event(
        request: AuthorizationRequest,
        ctx: Context,
        decision: PolicyDecision,
        denial_code: str | None,
    ) -> EventEnvelope:
        request_hash = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
        return EventEnvelope(
            event_id=f"aud_{uuid.uuid4().hex}",
            occurred_at=datetime.now(UTC),
            tenant_id=getattr(request.actor, "tenant_id", request.resource.tenant_id),
            actor=getattr(request.actor, "subject_id", "unknown"),
            action=request.action,
            resource=request.resource,
            decision="allow" if decision.allow else "deny",
            policy_version=decision.policy_version,
            args_hash=f"sha256:{request_hash}",
            result="success" if decision.allow else "denied",
            correlation_id=(
                request.context.correlation_id
                or getattr(ctx, "correlation_id", request.context.correlation_id)
            ),
            payload={"reason_code": denial_code or "allowed"},
        )
