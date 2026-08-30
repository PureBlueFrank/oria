"""Immutable approval bindings and canonical tool-argument hashing."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence, Set
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Literal, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

from oria.core.types import ValueModel

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
        decided = self.status in {"approved", "rejected"}
        if decided != bool(self.decider and self.decision and self.decided_at):
            raise ValueError("approval decision fields must match its status")
        if self.status == "approved" and self.decision != "approve":
            raise ValueError("approved status requires an approve decision")
        if self.status == "rejected" and self.decision != "reject":
            raise ValueError("rejected status requires a reject decision")
        if self.status in {"pending", "expired", "invalidated"} and any(
            (self.decider, self.decision, self.decided_at)
        ):
            raise ValueError("undecided approval states cannot carry decision fields")
        return self
