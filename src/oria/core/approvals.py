"""Immutable approval bindings and canonical tool-argument hashing."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence, Set
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    PolicyDecision,
    ResourceRef,
    ToolPolicy,
    ValueModel,
)

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.core.protocols import PolicyEngine

ApprovalAction: TypeAlias = Literal["launch_approval", "consumer_publish_approval"]


def _decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Decimal values must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize(value: object, path: str = "args") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite floats")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} must include a timezone")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, time):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} must include a timezone")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize(value.value, path)
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python"), path)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            normalized[key] = _normalize(item, f"{path}.{key}")
        return normalized
    if isinstance(value, Set):
        items = [_normalize(item, f"{path}[]") for item in value]
        return sorted(items, key=_canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item, f"{path}[]") for item in value]
    raise ValueError(f"{path} contains a value that cannot be canonicalized")


def canonical_args_hash(
    *,
    tool_name: str,
    tool_schema_version: int,
    schema: type[BaseModel],
    args: Mapping[str, object],
) -> str:
    """Validate and hash semantic tool arguments using the ADR-024 encoding."""
    if not tool_name:
        raise ValueError("tool_name must be non-empty")
    if tool_schema_version < 1:
        raise ValueError("tool_schema_version must be positive")
    unknown_fields = set(args).difference(schema.model_fields)
    if unknown_fields:
        raise ValueError("tool arguments contain unknown fields")
    validated = schema.model_validate(dict(args))
    normalized_args = _normalize(validated.model_dump(mode="python"))
    document = {
        "normalized_args": normalized_args,
        "tool_name": tool_name,
        "tool_schema_version": tool_schema_version,
    }
    digest = hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class Approval(ValueModel):
    """One immutable approval state bound to an exact execution resume point."""

    approval_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1, repr=False)
    approval_action: ApprovalAction
    tool_name: str = Field(min_length=1)
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    expires_at: datetime
    status: Literal["pending", "approved", "rejected", "expired", "invalidated"] = "pending"
    requester: str = Field(min_length=1, repr=False)
    decider: str | None = Field(default=None, min_length=1, repr=False)
    decision: Literal["approve", "reject"] | None = None
    reason: str | None = Field(default=None, max_length=1000)
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None

    @field_validator("expires_at", "created_at", "updated_at", "decided_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None, info: object) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> Approval:
        if self.updated_at < self.created_at or self.expires_at <= self.created_at:
            raise ValueError("approval timestamps are out of order")
        decision_values = (self.decider, self.decision, self.decided_at)
        has_decision = any(decision_values)
        complete_decision = all(decision_values)
        if has_decision != complete_decision:
            raise ValueError("approval decision fields must match its status")
        if self.status in {"approved", "rejected"} and not complete_decision:
            raise ValueError("approval decision fields must match its status")
        if self.status == "approved" and self.decision != "approve":
            raise ValueError("approved status requires an approve decision")
        if self.status == "rejected" and self.decision != "reject":
            raise ValueError("rejected status requires a reject decision")
        if self.status == "pending" and has_decision:
            raise ValueError("undecided approval states cannot carry decision fields")
        return self


class ApprovalResumeRequest(ValueModel):
    """Current execution facts that must match an approval before resume."""

    approval_id: str | None = Field(default=None, min_length=1)
    approval_action: ApprovalAction
    tool_name: str = Field(min_length=1)
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    approval_required: bool = False


class ApprovalRepository(Protocol):
    async def add(self, approval: Approval) -> None: ...

    async def get(self, tenant_id: str, approval_id: str) -> Approval | None: ...

    async def replace(self, approval: Approval) -> None: ...


class InMemoryApprovalRepository:
    """Deterministic repository used by local orchestration and unit tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Approval] = {}

    async def add(self, approval: Approval) -> None:
        key = (approval.tenant_id, approval.approval_id)
        if key in self._items:
            raise ValueError("approval already exists")
        self._items[key] = approval

    async def get(self, tenant_id: str, approval_id: str) -> Approval | None:
        return self._items.get((tenant_id, approval_id))

    async def replace(self, approval: Approval) -> None:
        key = (approval.tenant_id, approval.approval_id)
        if key not in self._items:
            raise LookupError("approval is unavailable")
        self._items[key] = approval


_REQUEST_ACTIONS: dict[ApprovalAction, str] = {
    "launch_approval": "approval:launch:request",
    "consumer_publish_approval": "approval:consumer_publish:request",
}
_DECIDE_ACTIONS: dict[ApprovalAction, str] = {
    "launch_approval": "approval:launch:decide",
    "consumer_publish_approval": "approval:consumer_publish:decide",
}


class ApprovalService:
    """Create, decide and consume exact approval bindings after authorization."""

    def __init__(
        self,
        repository: ApprovalRepository,
        policy: PolicyEngine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: f"approval_{uuid.uuid4().hex}",
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock
        self._id_factory = id_factory

    async def create(
        self,
        *,
        approval_action: ApprovalAction,
        tool_name: str,
        canonical_args_hash: str,
        checkpoint_id: str,
        expires_at: datetime,
        ctx: Context,
    ) -> Approval:
        now = self._now()
        if expires_at <= now:
            raise ValueError("approval expiry must be in the future")
        decision = await self._authorize(
            action=_REQUEST_ACTIONS[approval_action],
            resource_id=checkpoint_id,
            ctx=ctx,
        )
        approval = Approval(
            approval_id=self._id_factory(),
            tenant_id=ctx.tenant_id,
            approval_action=approval_action,
            tool_name=tool_name,
            canonical_args_hash=canonical_args_hash,
            checkpoint_id=checkpoint_id,
            policy_version=decision.policy_version,
            expires_at=expires_at,
            requester=ctx.actor.subject_id,
            created_at=now,
            updated_at=now,
        )
        await self._repository.add(approval)
        return approval

    async def decide(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        decision: Literal["approve", "reject"],
        reason: str | None,
        ctx: Context,
    ) -> Approval:
        if tenant_id != ctx.tenant_id:
            raise PermissionError("cross-tenant approval access is denied")
        approval = await self._required(tenant_id, approval_id)
        if approval.status != "pending":
            raise ValueError("only pending approvals can be decided")
        if approval.requester == ctx.actor.subject_id:
            raise PermissionError("approval requester cannot decide the same approval")
        if decision == "reject" and not reason:
            raise ValueError("approval rejection requires a reason")
        authorization = await self._authorize(
            action=_DECIDE_ACTIONS[approval.approval_action],
            resource_id=approval.approval_id,
            ctx=ctx,
        )
        if authorization.policy_version != approval.policy_version:
            invalidated = self._terminalize(approval, status="invalidated")
            await self._repository.replace(invalidated)
            raise PermissionError("approval policy version changed")
        now = self._now()
        if now >= approval.expires_at:
            expired = self._terminalize(approval, status="expired")
            await self._repository.replace(expired)
            raise PermissionError("approval has expired")
        decided = Approval.model_validate(
            {
                **approval.model_dump(),
                "status": "approved" if decision == "approve" else "rejected",
                "decider": ctx.actor.subject_id,
                "decision": decision,
                "reason": reason,
                "updated_at": now,
                "decided_at": now,
            }
        )
        await self._repository.replace(decided)
        return decided

    async def authorize_resume(
        self,
        *,
        request: ApprovalResumeRequest,
        tool_policy: ToolPolicy,
        ctx: Context,
    ) -> Approval | None:
        """Reauthorize the current actor and validate every frozen binding field."""
        authorization = await self._authorize(
            action=tool_policy.required_action,
            resource_id=request.tool_name,
            ctx=ctx,
            resource_type=tool_policy.resource_type,
        )
        required = tool_policy.approval_mode == "required" or (
            tool_policy.approval_mode == "conditional" and request.approval_required
        )
        if not required:
            return None
        if not tool_policy.side_effect:
            raise ValueError("approval cannot be required for a read-only tool")
        if request.approval_id is None:
            raise PermissionError("approved binding is required before side-effect execution")
        approval = await self._required(ctx.tenant_id, request.approval_id)
        current = (
            request.approval_action,
            request.tool_name,
            request.canonical_args_hash,
            request.checkpoint_id,
            authorization.policy_version,
        )
        frozen = (
            approval.approval_action,
            approval.tool_name,
            approval.canonical_args_hash,
            approval.checkpoint_id,
            approval.policy_version,
        )
        if current != frozen or tool_policy.approval_action != request.approval_action:
            invalidated = self._terminalize(approval, status="invalidated")
            await self._repository.replace(invalidated)
            raise PermissionError("approval binding no longer matches the execution")
        now = self._now()
        if now >= approval.expires_at:
            expired = self._terminalize(approval, status="expired")
            await self._repository.replace(expired)
            raise PermissionError("approval has expired")
        if approval.status != "approved":
            raise PermissionError("approval is not approved")
        return approval

    async def _authorize(
        self,
        *,
        action: str,
        resource_id: str,
        ctx: Context,
        resource_type: str = "approval",
    ) -> PolicyDecision:
        decision = await self._policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=action,
                resource=ResourceRef(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant_id=ctx.tenant_id,
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            ),
            ctx,
        )
        if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
            raise PermissionError("approval operation is not authorized")
        return decision

    async def _required(self, tenant_id: str, approval_id: str) -> Approval:
        approval = await self._repository.get(tenant_id, approval_id)
        if approval is None:
            raise LookupError("approval is unavailable")
        return approval

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("approval clock must return a timezone-aware timestamp")
        return now

    def _terminalize(
        self,
        approval: Approval,
        *,
        status: Literal["expired", "invalidated"],
    ) -> Approval:
        return Approval.model_validate(
            {
                **approval.model_dump(),
                "status": status,
                "updated_at": self._now(),
            }
        )
