"""V0.3-T05 timeout and atomic-retry recovery coverage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import NOW, enrollment_harness

from oria.domain.business import EnrollmentItem
from oria.domain.confirmations import BusinessConfirmationPolicy
from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    UpsertEnrollmentItemsArgs,
)
from oria.domain.enrollment_branch import EnrollmentBranchCoordinator, EnrollmentBranchState
from oria.storage.repositories import BusinessRepositoryError

pytestmark = pytest.mark.recovery


@pytest.mark.asyncio
async def test_window_clock_timeout_closes_merchant_side_and_allows_hybrid_join(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, mode="hybrid") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        ).model_copy(update={"auto_completed": True})
        coordinator = EnrollmentBranchCoordinator(
            inbox=None,  # type: ignore[arg-type]
            enrollments=harness.enrollments,
            approval_invalidator=None,  # type: ignore[arg-type]
            clock=lambda: state.enrollment_window_end + timedelta(seconds=1),
        )

        timed_out = coordinator.resolve_window_timeout(state)

    assert timed_out.status == "window_timeout"
    assert timed_out.state.window_closed is True
    assert timed_out.state.join_complete is True


def test_confirmation_timeout_action_is_resolved_from_the_frozen_policy() -> None:
    from tests.support.enrollment import snapshot

    frozen = snapshot(confirmation_steps=("merchant",))
    policy = BusinessConfirmationPolicy.from_snapshot(frozen)
    item = EnrollmentItem(
        tenant_id="local-community",
        enrollment_item_id="item-1",
        enrollment_id="enrollment-1",
        campaign_id="campaign-1",
        merchant_id="demo-m001",
        product_ref="product-1",
        product_version="v1",
        product_snapshot_id="snapshot-1",
        mode="merchant",
        sources=frozenset({"merchant"}),
        status="pending_confirmation",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    task = policy.generate_tasks(
        enrollment_item=item,
        subject_ids={"merchant": "demo-m001"},
        created_at=NOW,
        due_at=NOW + timedelta(hours=1),
    )[0]

    resolved = policy.resolve_timeout(task, updated_at=NOW + timedelta(hours=2))

    assert resolved.status == "rejected"


@pytest.mark.asyncio
async def test_coupon_link_partial_validation_failure_rolls_back_every_link(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        upserted = await harness.enrollments.upsert_items(
            UpsertEnrollmentItemsArgs(
                campaign_id="campaign-1",
                source="auto",
                items=(
                    EnrollmentItemInput(
                        merchant_id="demo-m001",
                        product_ref="product-1",
                        product_version="v1",
                    ),
                ),
                idempotency_key="auto-1",
            ),
            harness.ctx,  # type: ignore[arg-type]
        )
        valid_id = upserted.enrollment_items[0].enrollment_item_id
        with pytest.raises(BusinessRepositoryError, match="does not match"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(valid_id, "missing-item"),
                    coupon_batch_id="coupon-1",
                    tier_mapping={valid_id: "base", "missing-item": "boosted"},
                    idempotency_key="partial-link-1",
                ),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_coupon_links"))

    assert count == 0
