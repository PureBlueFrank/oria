"""V0.3-T02 approval bindings and canonical argument hashing tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import Field, ValidationError

from oria.core.approvals import Approval, canonical_args_hash
from oria.core.types import ValueModel

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


class _LaunchArgs(ValueModel):
    amount: Decimal = Field(gt=0)
    ratio: Decimal = Field(gt=0, lt=1)
    starts_at: datetime
    merchant_ids: frozenset[str]


def _hash(args: dict[str, object], *, version: int = 1) -> str:
    return canonical_args_hash(
        tool_name="LaunchPlan",
        tool_schema_version=version,
        schema=_LaunchArgs,
        args=args,
    )


def test_canonical_hash_ignores_json_key_whitespace_decimal_and_timezone_spelling() -> None:
    first = json.loads(
        '{"amount":"10.00","ratio":"0.100","starts_at":"2026-08-30T18:00:00+08:00",'
        '"merchant_ids":["merchant-b","merchant-a"]}'
    )
    second = json.loads(
        '{ "merchant_ids": ["merchant-a", "merchant-b"], "starts_at": '
        '"2026-08-30T10:00:00Z", "ratio": "0.1", "amount": "10" }'
    )

    assert _hash(first) == _hash(second)


def test_canonical_hash_changes_for_semantic_amount_time_or_schema_version() -> None:
    base = {
        "amount": "10",
        "ratio": "0.1",
        "starts_at": "2026-08-30T10:00:00Z",
        "merchant_ids": ["merchant-a"],
    }

    assert _hash(base) != _hash({**base, "amount": "11"})
    assert _hash(base) != _hash({**base, "starts_at": "2026-08-30T10:01:00Z"})
    assert _hash(base) != _hash(base, version=2)


@pytest.mark.parametrize(
    "args",
    [
        {
            "amount": "10",
            "ratio": "0.1",
            "starts_at": "2026-08-30T10:00:00",
            "merchant_ids": [],
        },
        {
            "amount": "NaN",
            "ratio": "0.1",
            "starts_at": "2026-08-30T10:00:00Z",
            "merchant_ids": [],
        },
        {
            "amount": "Infinity",
            "ratio": "0.1",
            "starts_at": "2026-08-30T10:00:00Z",
            "merchant_ids": [],
        },
        {
            "amount": "10",
            "ratio": "0.1",
            "starts_at": "2026-08-30T10:00:00Z",
            "merchant_ids": [],
            "untrusted": True,
        },
    ],
)
def test_canonical_hash_rejects_naive_non_finite_and_unknown_values(
    args: dict[str, object],
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        _hash(args)


def test_approval_value_requires_exact_decision_state_and_aware_binding() -> None:
    pending = Approval(
        approval_id="approval-1",
        tenant_id="tenant-a",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash="sha256:" + "a" * 64,
        checkpoint_id="checkpoint-1",
        policy_version="policy-v1",
        expires_at=NOW + timedelta(hours=1),
        requester="requester-a",
        created_at=NOW,
        updated_at=NOW,
    )

    assert pending.status == "pending"
    with pytest.raises(ValidationError, match="decision fields"):
        Approval.model_validate({**pending.model_dump(), "status": "approved"})
    with pytest.raises(ValidationError, match="timezone"):
        Approval.model_validate(
            {
                **pending.model_dump(),
                "expires_at": datetime(2026, 8, 30, 12, 0),
            }
        )
