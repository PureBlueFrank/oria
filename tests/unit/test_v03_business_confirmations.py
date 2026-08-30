"""V0.3-T02 dynamic business confirmation policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oria.domain.business import EnrollmentItem
from oria.domain.confirmations import BusinessConfirmationPolicy
from oria.domain.models import ConfirmationRule
from oria.rag.models import CampaignRuleSnapshot
from oria.resources.loader import load_demo_data

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


def _item() -> EnrollmentItem:
    return EnrollmentItem(
        tenant_id="tenant-a",
        enrollment_item_id="item-1",
        version=1,
        created_at=NOW,
        updated_at=NOW,
        enrollment_id="enrollment-1",
        campaign_id="campaign-1",
        merchant_id="merchant-1",
        product_ref="product-1",
        product_version="v1",
        product_snapshot_id="snapshot-1",
        mode="hybrid",
        sources=frozenset({"merchant"}),
        status="pending_confirmation",
    )


def _snapshot(rule: ConfirmationRule) -> CampaignRuleSnapshot:
    demo = load_demo_data().rules
    placeholder = CampaignRuleSnapshot(
        snapshot_id="rs_123456789012345678901234",
        snapshot_hash="sha256:" + "0" * 64,
        tenant_id="tenant-a",
        effective_at=NOW,
        basic=demo.basic,
        recruitment_scope=demo.recruitment_scope,
        enrollment_policy=demo.enrollment_policy,
        benefit_policy=demo.benefit_policy,
        confirmation_policy=rule,
        merchant_material=demo.merchant_material,
        field_evidence={},
    )
    return placeholder.model_copy(update={"snapshot_hash": placeholder.recompute_hash()})


def test_snapshot_rule_generates_zero_or_ordered_multilevel_tasks() -> None:
    empty = BusinessConfirmationPolicy.from_snapshot(
        _snapshot(ConfirmationRule(ordered_steps=(), timeout_action="reject"))
    )
    assert (
        empty.generate_tasks(
            enrollment_item=_item(), subject_ids={}, created_at=NOW, due_at=NOW + timedelta(hours=1)
        )
        == ()
    )

    policy = BusinessConfirmationPolicy.from_snapshot(
        _snapshot(
            ConfirmationRule(
                ordered_steps=("merchant", "sales", "sales_manager"),
                timeout_action="escalate",
            )
        )
    )
    tasks = policy.generate_tasks(
        enrollment_item=_item(),
        subject_ids={
            "merchant": "merchant-1",
            "sales": "sales-1",
            "sales_manager": "manager-1",
        },
        created_at=NOW,
        due_at=NOW + timedelta(hours=1),
    )

    assert tuple(task.subject_type for task in tasks) == ("merchant", "sales", "sales_manager")
    assert tuple(task.sequence for task in tasks) == (1, 2, 3)
    assert len({task.confirmation_task_id for task in tasks}) == 3

    with pytest.raises(ValidationError, match="ordered by responsibility"):
        ConfirmationRule(ordered_steps=("sales_manager", "merchant"), timeout_action="reject")


@pytest.mark.parametrize(
    ("timeout_action", "expected_status"),
    [("reject", "rejected"), ("escalate", "timed_out")],
)
def test_timeout_rejects_or_escalates_without_default_auto_pass(
    timeout_action: str,
    expected_status: str,
) -> None:
    rule = ConfirmationRule(ordered_steps=("merchant",), timeout_action=timeout_action)  # type: ignore[arg-type]
    policy = BusinessConfirmationPolicy.from_snapshot(_snapshot(rule))
    task = policy.generate_tasks(
        enrollment_item=_item(),
        subject_ids={"merchant": "merchant-1"},
        created_at=NOW,
        due_at=NOW + timedelta(hours=1),
    )[0]

    resolved = policy.resolve_timeout(task, updated_at=NOW + timedelta(hours=2))

    assert resolved.status == expected_status


def test_explicit_auto_confirm_requires_a_separate_positive_authorization() -> None:
    policy = BusinessConfirmationPolicy.from_snapshot(
        _snapshot(
            ConfirmationRule(ordered_steps=("merchant",), timeout_action="explicit_auto_confirm")
        )
    )
    task = policy.generate_tasks(
        enrollment_item=_item(),
        subject_ids={"merchant": "merchant-1"},
        created_at=NOW,
        due_at=NOW + timedelta(hours=1),
    )[0]

    with pytest.raises(PermissionError, match="requires policy authorization"):
        policy.resolve_timeout(task, updated_at=NOW + timedelta(hours=2))
    assert (
        policy.resolve_timeout(
            task,
            updated_at=NOW + timedelta(hours=2),
            auto_confirm_authorized=True,
        ).status
        == "confirmed"
    )
