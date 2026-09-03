"""Unit tests for V0.3 execution-ledger values and launch-plan hashes."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oria.domain.ledger import (
    DomainEvent,
    LaunchChildStep,
    LaunchPlan,
    OutboxRecord,
    Receipt,
    ToolExecution,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
HASH_A = f"sha256:{'a' * 64}"
HASH_B = f"sha256:{'b' * 64}"
HASH_C = f"sha256:{'c' * 64}"


def _reserved() -> ToolExecution:
    return ToolExecution(
        execution_id="exec_1",
        tenant_id="tenant_a",
        tool_name="publish_recruitment",
        idempotency_key="campaign_1:publish:" + HASH_A,
        canonical_args_hash=HASH_A,
        checkpoint_id="checkpoint_1",
        created_at=NOW,
        updated_at=NOW,
    )


def _plan_hash(steps: list[LaunchChildStep]) -> str:
    return LaunchPlan.compute_plan_hash(
        child_steps=steps,
        campaign_draft_id="campaign_1",
        campaign_draft_hash=HASH_A,
        rule_snapshot_id="rule_1",
        rule_snapshot_hash=HASH_B,
        coupon_batch_draft_id="coupon_1",
        coupon_batch_draft_hash=HASH_C,
        merchant_scope_hash=HASH_A,
        material_version="material_v1",
        compensation_policy_version="compensation_v1",
    )


def test_tool_execution_accepts_only_declared_state_transitions() -> None:
    reserved = _reserved()
    executing = reserved.transition_to("executing", updated_at=NOW + timedelta(seconds=1))
    succeeded = executing.transition_to(
        "succeeded",
        updated_at=NOW + timedelta(seconds=2),
        receipt_id="receipt_1",
    )

    assert executing.attempt_count == 1
    assert succeeded.status == "succeeded"
    assert succeeded.executed_at == NOW + timedelta(seconds=2)
    with pytest.raises(ValueError, match="reserved -> succeeded"):
        reserved.transition_to(
            "succeeded",
            updated_at=NOW + timedelta(seconds=1),
            receipt_id="receipt_1",
        )
    with pytest.raises(ValueError, match="succeeded -> executing"):
        succeeded.transition_to("executing", updated_at=NOW + timedelta(seconds=3))


def test_unknown_can_only_reconcile_to_terminal_outcome() -> None:
    executing = _reserved().transition_to("executing", updated_at=NOW + timedelta(seconds=1))
    unknown = executing.transition_to("unknown", updated_at=NOW + timedelta(seconds=2))

    with pytest.raises(ValueError, match="unknown -> executing"):
        unknown.transition_to("executing", updated_at=NOW + timedelta(seconds=3))
    reconciled = unknown.transition_to(
        "succeeded",
        updated_at=NOW + timedelta(seconds=3),
        receipt_id="receipt_1",
    )
    assert reconciled.status == "succeeded"
    assert reconciled.attempt_count == 1


def test_tool_execution_validates_receipt_and_timestamp_invariants() -> None:
    values = _reserved().model_dump()
    with pytest.raises(ValidationError, match="succeeded executions require a receipt"):
        ToolExecution.model_validate(
            values
            | {
                "status": "succeeded",
                "attempt_count": 1,
                "updated_at": NOW + timedelta(seconds=1),
                "executed_at": NOW + timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="summary hashes require a receipt identity"):
        ToolExecution.model_validate(
            values
            | {
                "status": "failed",
                "attempt_count": 1,
                "receipt_summary_hash": HASH_A,
                "updated_at": NOW + timedelta(seconds=1),
                "executed_at": NOW + timedelta(seconds=1),
            }
        )
    rejected = ToolExecution.model_validate(
        values
        | {
            "status": "failed",
            "attempt_count": 1,
            "receipt_id": "receipt_1",
            "receipt_summary_hash": HASH_A,
            "updated_at": NOW + timedelta(seconds=1),
            "executed_at": NOW + timedelta(seconds=1),
        }
    )
    assert rejected.receipt_id == "receipt_1"


def test_launch_plan_hash_is_order_independent_and_binds_every_child_argument() -> None:
    materialize = LaunchChildStep(
        tool_name="materialize_coupon_batch",
        canonical_args_hash=HASH_A,
        idempotency_scope="campaign_1:coupon",
    )
    publish = LaunchChildStep(
        tool_name="publish_recruitment",
        canonical_args_hash=HASH_B,
        idempotency_scope="campaign_1:publish",
    )

    assert _plan_hash([materialize, publish]) == _plan_hash([publish, materialize])
    changed = publish.model_copy(update={"canonical_args_hash": HASH_C})
    assert _plan_hash([materialize, publish]) != _plan_hash([materialize, changed])


def test_launch_plan_rejects_a_hash_that_does_not_match_its_binding() -> None:
    materialize = LaunchChildStep(
        tool_name="materialize_coupon_batch",
        canonical_args_hash=HASH_A,
        idempotency_scope="campaign_1:coupon",
    )
    publish = LaunchChildStep(
        tool_name="publish_recruitment",
        canonical_args_hash=HASH_B,
        idempotency_scope="campaign_1:publish",
    )
    with pytest.raises(ValidationError, match="plan_hash does not match"):
        LaunchPlan(
            campaign_draft_id="campaign_1",
            campaign_draft_hash=HASH_A,
            rule_snapshot_id="rule_1",
            rule_snapshot_hash=HASH_B,
            coupon_batch_draft_id="coupon_1",
            coupon_batch_draft_hash=HASH_C,
            merchant_scope_hash=HASH_A,
            material_version="material_v1",
            child_steps=[materialize, publish],
            compensation_policy_version="compensation_v1",
            plan_hash=HASH_A,
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("campaign_draft_id", "campaign_2"),
        ("campaign_draft_hash", HASH_C),
        ("rule_snapshot_id", "rule_2"),
        ("rule_snapshot_hash", HASH_C),
        ("coupon_batch_draft_id", "coupon_2"),
        ("coupon_batch_draft_hash", HASH_A),
        ("merchant_scope_hash", HASH_C),
        ("material_version", "material_v2"),
        ("compensation_policy_version", "compensation_v2"),
    ],
)
def test_launch_plan_hash_changes_with_every_top_level_binding(
    field: str,
    changed: str,
) -> None:
    steps = [
        LaunchChildStep(
            tool_name="materialize_coupon_batch",
            canonical_args_hash=HASH_A,
            idempotency_scope="campaign_1:coupon",
        ),
        LaunchChildStep(
            tool_name="publish_recruitment",
            canonical_args_hash=HASH_B,
            idempotency_scope="campaign_1:publish",
        ),
    ]
    values = {
        "campaign_draft_id": "campaign_1",
        "campaign_draft_hash": HASH_A,
        "rule_snapshot_id": "rule_1",
        "rule_snapshot_hash": HASH_B,
        "coupon_batch_draft_id": "coupon_1",
        "coupon_batch_draft_hash": HASH_C,
        "merchant_scope_hash": HASH_A,
        "material_version": "material_v1",
        "compensation_policy_version": "compensation_v1",
        "child_steps": steps,
    }

    original = LaunchPlan.compute_plan_hash(**values)  # type: ignore[arg-type]
    mutated = LaunchPlan.compute_plan_hash(**(values | {field: changed}))  # type: ignore[arg-type]

    assert mutated != original


def test_domain_event_outbox_and_receipt_reject_untrusted_shapes() -> None:
    event = DomainEvent(
        event_id="event_1",
        tenant_id="tenant_a",
        aggregate_type="campaign",
        aggregate_id="campaign_1",
        event_type="campaign.launched",
        event_version=1,
        payload={"campaign_hash": HASH_A},
        occurred_at=NOW,
        correlation_id="correlation_1",
    )
    outbox = OutboxRecord(
        event_id=event.event_id,
        tenant_id=event.tenant_id,
        topic=event.event_type,
        payload_json='{"z":1, "a":"redacted"}',
        occurred_at=NOW,
        available_at=NOW,
    )
    receipt = Receipt(
        receipt_id="receipt_1",
        adapter_id="mock_recruitment",
        resource_ref="campaign:campaign_1",
        status="accepted",
        received_at=NOW,
        summary_hash=HASH_B,
    )

    assert outbox.payload_json == '{"a":"redacted","z":1}'
    assert receipt.summary_hash == HASH_B
    with pytest.raises(ValidationError, match="timezone"):
        event.model_copy(update={"occurred_at": NOW.replace(tzinfo=None)}).model_validate(
            event.model_dump() | {"occurred_at": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError, match="JSON object"):
        OutboxRecord(
            event_id="event_2",
            tenant_id="tenant_a",
            topic="campaign.launched",
            payload_json="[]",
            occurred_at=NOW,
            available_at=NOW,
        )
    with pytest.raises(ValidationError, match="summary_hash"):
        Receipt(
            receipt_id="receipt_2",
            adapter_id="mock_recruitment",
            resource_ref="campaign:campaign_1",
            status="unknown",
            received_at=NOW,
            summary_hash="unsafe summary",
        )
