"""Business execution-ledger values and immutable launch-plan bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from oria.core.approvals import _canonical_json, _normalize
from oria.core.types import JsonValue, ValueModel

ExecutionStatus = Literal["reserved", "executing", "succeeded", "failed", "unknown"]

_EXECUTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "reserved": frozenset({"executing"}),
    "executing": frozenset({"succeeded", "failed", "unknown"}),
    "unknown": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
}


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class ToolExecution(ValueModel):
    """One tenant-scoped reservation and its external side-effect outcome."""

    execution_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1, repr=False)
    tool_name: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    status: ExecutionStatus = "reserved"
    receipt_id: str | None = Field(default=None, min_length=1)
    compensation_status: str | None = Field(default=None, min_length=1)
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    executed_at: datetime | None = None

    @field_validator("created_at", "updated_at", "executed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, str(getattr(info, "field_name", "timestamp")))

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.executed_at is not None and self.executed_at < self.created_at:
            raise ValueError("executed_at must not precede created_at")
        if self.status == "reserved":
            if (
                self.attempt_count != 0
                or self.executed_at is not None
                or self.receipt_id is not None
            ):
                raise ValueError("reserved executions cannot contain attempt or outcome data")
        elif self.status == "executing":
            if (
                self.attempt_count < 1
                or self.executed_at is not None
                or self.receipt_id is not None
            ):
                raise ValueError("executing executions require an attempt without outcome data")
        else:
            if self.attempt_count < 1 or self.executed_at is None:
                raise ValueError("terminal executions require attempt and execution timestamps")
            if self.status == "succeeded" and self.receipt_id is None:
                raise ValueError("succeeded executions require a receipt")
            if self.status == "failed" and self.receipt_id is not None:
                raise ValueError("failed executions cannot carry a receipt")
        return self

    def transition_to(
        self,
        target: ExecutionStatus,
        *,
        updated_at: datetime,
        receipt_id: str | None = None,
        compensation_status: str | None = None,
    ) -> ToolExecution:
        """Return the validated next ledger state; unknown never re-enters execution."""
        _require_aware(updated_at, "updated_at")
        if target not in _EXECUTION_TRANSITIONS[self.status]:
            raise ValueError(f"illegal execution transition: {self.status} -> {target}")
        if updated_at < self.updated_at:
            raise ValueError("updated_at must not move backwards")
        updates: dict[str, object] = {
            "status": target,
            "updated_at": updated_at,
            "receipt_id": receipt_id,
            "compensation_status": compensation_status,
        }
        if target == "executing":
            updates["attempt_count"] = self.attempt_count + 1
            updates["executed_at"] = None
        else:
            updates["executed_at"] = updated_at
        return ToolExecution.model_validate(self.model_dump() | updates)


class DomainEvent(ValueModel):
    """Append-only, redacted business fact."""

    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1, repr=False)
    aggregate_type: str = Field(min_length=1)
    aggregate_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    event_version: int = Field(ge=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    occurred_at: datetime
    correlation_id: str = Field(min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def require_aware_occurred_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


class OutboxRecord(ValueModel):
    """Business-database event awaiting idempotent publication."""

    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1, repr=False)
    topic: str = Field(min_length=1)
    payload_json: str = Field(min_length=2, repr=False)
    occurred_at: datetime
    available_at: datetime
    published_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, min_length=1)

    @field_validator("payload_json")
    @classmethod
    def require_canonical_object_json(cls, value: str) -> str:
        try:
            payload = json.loads(
                value,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant: {constant}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("payload_json must contain finite JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("payload_json must contain a JSON object")
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @field_validator("occurred_at", "available_at", "published_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, str(getattr(info, "field_name", "timestamp")))

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.available_at < self.occurred_at:
            raise ValueError("available_at must not precede occurred_at")
        if self.published_at is not None and self.published_at < self.occurred_at:
            raise ValueError("published_at must not precede occurred_at")
        return self


class Receipt(ValueModel):
    """Secret-free summary of an external side-effect response."""

    receipt_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    resource_ref: str = Field(min_length=1)
    external_id: str | None = Field(default=None, min_length=1, repr=False)
    status: Literal["accepted", "unknown", "rejected"]
    received_at: datetime
    summary_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", repr=False)

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "received_at")


class LaunchChildStep(ValueModel):
    tool_name: str = Field(min_length=1)
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_scope: str = Field(min_length=1)


class LaunchPlan(ValueModel):
    """Immutable composite approval binding; child idempotency remains independent."""

    campaign_draft_id: str = Field(min_length=1)
    campaign_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rule_snapshot_id: str = Field(min_length=1)
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coupon_batch_draft_id: str = Field(min_length=1)
    coupon_batch_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    merchant_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    material_version: str = Field(min_length=1)
    child_steps: list[LaunchChildStep] = Field(min_length=1)
    compensation_policy_version: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def compute_plan_hash(
        cls,
        *,
        child_steps: Sequence[LaunchChildStep],
        campaign_draft_hash: str,
        rule_snapshot_hash: str,
        coupon_batch_draft_hash: str,
        merchant_scope_hash: str,
        material_version: str,
        compensation_policy_version: str,
    ) -> str:
        ordered_steps = sorted(child_steps, key=lambda step: step.tool_name)
        document = {
            "campaign_draft_hash": campaign_draft_hash,
            "child_steps": [
                {
                    "canonical_args_hash": step.canonical_args_hash,
                    "idempotency_scope": step.idempotency_scope,
                    "tool_name": step.tool_name,
                }
                for step in ordered_steps
            ],
            "compensation_policy_version": compensation_policy_version,
            "coupon_batch_draft_hash": coupon_batch_draft_hash,
            "material_version": material_version,
            "merchant_scope_hash": merchant_scope_hash,
            "rule_snapshot_hash": rule_snapshot_hash,
        }
        normalized = _normalize(document, "launch_plan")
        digest = hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @model_validator(mode="after")
    def validate_plan_hash(self) -> Self:
        names = [step.tool_name for step in self.child_steps]
        if len(names) != len(set(names)):
            raise ValueError("launch plan child tool names must be unique")
        expected = self.compute_plan_hash(
            child_steps=self.child_steps,
            campaign_draft_hash=self.campaign_draft_hash,
            rule_snapshot_hash=self.rule_snapshot_hash,
            coupon_batch_draft_hash=self.coupon_batch_draft_hash,
            merchant_scope_hash=self.merchant_scope_hash,
            material_version=self.material_version,
            compensation_policy_version=self.compensation_policy_version,
        )
        if self.plan_hash != expected:
            raise ValueError("plan_hash does not match the launch plan binding")
        return self
