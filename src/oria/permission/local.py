"""Deny-by-default policy for the trusted community-local identity profile."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
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
        "merchant:read",
        "rule:read",
    }
)
_DOCUMENT_READ_ACTIONS = frozenset({"document:read", "rule:read"})
_WRITE_ACTION_ROLES: dict[str, frozenset[str]] = {
    "ingress:submit": frozenset({"operator"}),
    "knowledge:delete": frozenset({"operator"}),
    "knowledge:write": frozenset({"operator"}),
    "campaign:draft:write": frozenset({"campaign_admin"}),
    "campaign:launch:request": frozenset({"campaign_admin"}),
    "approval:launch:request": frozenset({"campaign_admin"}),
    "approval:launch:decide": frozenset({"launch_approver"}),
    "approval:consumer_publish:request": frozenset({"campaign_admin"}),
    "approval:consumer_publish:decide": frozenset({"consumer_publish_approver"}),
    "confirmation:merchant:decide": frozenset({"merchant"}),
    "confirmation:sales:decide": frozenset({"sales"}),
    "confirmation:sales_manager:decide": frozenset({"sales_manager"}),
    "integration:event:ingest": frozenset({"integration_adapter"}),
}
_DENIAL_REASONS = {
    "missing_principals": "authorization principals are required",
    "context_mismatch": "authorization principals do not match the trusted context",
    "untrusted_principal": "principal is not the trusted community identity",
    "cross_tenant": "cross-tenant access is denied",
    "unknown_action": "action is not allowed by the local read policy",
    "role_denied": "actor role is not authorized for the write action",
    "assignment_denied": "confirmation task is not assigned to the actor",
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
    """Authorize trusted local identities with deny-by-default read/write RBAC."""

    def __init__(
        self,
        audit: AuditService | None = None,
        *,
        trusted_actors: Iterable[Principal] | None = None,
        trusted_executors: Iterable[Principal] | None = None,
        confirmation_assignments: Mapping[str, str] | None = None,
    ) -> None:
        self._audit = audit
        actors = tuple(trusted_actors) if trusted_actors is not None else (local_operator(),)
        executors = (
            tuple(trusted_executors) if trusted_executors is not None else (local_cli_executor(),)
        )
        self._trusted_actors = frozenset(actors)
        self._trusted_executors = frozenset(executors)
        self._confirmation_assignments = dict(confirmation_assignments or {})

    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision:
        denial_code = self._denial_code(request, ctx)
        allowed = denial_code is None
        acl_filter = None
        if allowed and request.action in _DOCUMENT_READ_ACTIONS:
            acl_filter = ACLFilter(
                tenant_id=request.actor.tenant_id,
                allowed_subject_ids=(request.actor.subject_id,),
                allowed_roles=request.actor.roles,
                classifications=("public", "internal", "restricted"),
            )
        decision = PolicyDecision(
            allow=allowed,
            constraints={"tenant_id": request.actor.tenant_id} if allowed else {},
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

    def _denial_code(self, request: AuthorizationRequest, ctx: Context) -> str | None:
        actor = getattr(request, "actor", None)
        executor = getattr(request, "executor", None)
        context_actor = getattr(ctx, "actor", None)
        context_executor = getattr(ctx, "executor", None)
        if actor is None or executor is None or context_actor is None or context_executor is None:
            return "missing_principals"
        if actor != context_actor or executor != context_executor:
            return "context_mismatch"
        if actor not in self._trusted_actors or executor not in self._trusted_executors:
            return "untrusted_principal"
        if actor.tenant_id != executor.tenant_id or request.resource.tenant_id != actor.tenant_id:
            return "cross_tenant"
        required_roles = _WRITE_ACTION_ROLES.get(request.action)
        if required_roles is not None and not required_roles.intersection(actor.roles):
            return "role_denied"
        if request.action.startswith("confirmation:"):
            assigned = self._confirmation_assignments.get(request.resource.resource_id)
            if assigned != actor.subject_id:
                return "assignment_denied"
        if request.action not in _LOCAL_ACTIONS and required_roles is None:
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
