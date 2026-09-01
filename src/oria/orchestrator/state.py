"""Serializable workflow state and conflict-detecting parallel reducers."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict, TypeVar, cast

from langgraph.graph.message import add_messages
from pydantic import BaseModel

from oria.core.types import NodeResult, ResourceRef

T = TypeVar("T")


class StateConflictError(RuntimeError):
    """A parallel/replayed update reused an identity with different content."""

    def __init__(self, key: str) -> None:
        super().__init__(f"workflow state conflict for key {key!r}")
        self.key = key


class Step(TypedDict):
    node_id: str
    params: dict[str, Any]


class Plan(TypedDict):
    goal: str
    steps: list[Step]


class RunMeta(TypedDict):
    tenant_id: str
    session_id: str
    thread_id: str
    run_id: str
    job_id: str | None
    requester_subject_id: str


class HitlState(TypedDict):
    approval_id: str
    step_id: str
    tool_name: str
    args_hash: str
    checkpoint_id: str
    policy_version: str
    requested_by: str
    requested_at: str
    expires_at: str
    resolved_by: str | None
    resolved_at: str | None
    decision: Literal["approve", "reject"] | None


class ExternalWaitState(TypedDict):
    wait_id: str
    step_id: str
    event_type: str
    resource: ResourceRef
    expected_version: str | None
    checkpoint_id: str
    correlation_token_hash: str
    requested_at: str
    expires_at: str
    timeout_action: Literal["resume", "fail", "cancel"]
    resolved_event_id: str | None
    resolved_at: str | None


class WorkflowState(TypedDict):
    messages: Annotated[list[Any], add_messages]
    plan: Plan
    results: Annotated[dict[str, NodeResult], merge_results]
    approvals: Annotated[dict[str, HitlState], merge_unique]
    external_waits: Annotated[dict[str, ExternalWaitState], merge_unique]
    meta: RunMeta


def _decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("workflow state contains a non-finite Decimal")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def canonical_json_value(value: object) -> object:
    """Convert supported values to deterministic JSON-mode Python values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("workflow state contains a non-finite float")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workflow state datetime must include a timezone")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, time):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workflow state time must include a timezone")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonical_json_value(value.value)
    if isinstance(value, BaseModel):
        return canonical_json_value(value.model_dump(mode="python", round_trip=True))
    if is_dataclass(value) and not isinstance(value, type):
        return canonical_json_value(asdict(value))
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("workflow state contains a non-string object key")
            normalized[key] = canonical_json_value(item)
        return normalized
    if isinstance(value, Set):
        normalized_items = [canonical_json_value(item) for item in value]
        return sorted(normalized_items, key=_canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_json_value(item) for item in value]
    raise ValueError("workflow state contains a value that cannot be canonicalized")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_unique_map(left: dict[str, T], right: dict[str, T]) -> dict[str, T]:
    """Merge only new identities or exact deterministic replays, without mutation."""

    merged = dict(left)
    for key, incoming in right.items():
        if key not in merged:
            merged[key] = incoming
            continue
        if canonical_json_value(merged[key]) != canonical_json_value(incoming):
            raise StateConflictError(key)
    return merged


def merge_results(
    left: dict[str, NodeResult], right: dict[str, NodeResult]
) -> dict[str, NodeResult]:
    return merge_unique_map(left, right)


def merge_unique(left: dict[str, T], right: dict[str, T]) -> dict[str, T]:
    return merge_unique_map(left, right)


def empty_workflow_state(*, plan: Plan, meta: RunMeta) -> WorkflowState:
    """Create the required serializable channels for a new workflow."""

    return cast(
        WorkflowState,
        {
            "messages": [],
            "plan": plan,
            "results": {},
            "approvals": {},
            "external_waits": {},
            "meta": meta,
        },
    )
