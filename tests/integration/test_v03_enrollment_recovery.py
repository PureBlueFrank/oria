"""V0.3-T05 timeout and atomic-retry recovery coverage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import NOW, auto_command, enrollment_harness

from oria.domain.business import EnrollmentItem
from oria.domain.confirmations import BusinessConfirmationPolicy
from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
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
        items = (
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        )
        upserted = await harness.enrollments.upsert_auto(
            auto_command(items, circle_run_id="auto-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        valid_id = upserted.enrollment_items[0].enrollment_item_id
        with pytest.raises(BusinessRepositoryError, match="unavailable"):
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


@pytest.mark.asyncio
async def test_local_write_failure_leaves_retryable_reservation_and_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        items = (
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        )
        upserted = await harness.enrollments.upsert_auto(
            auto_command(items, circle_run_id="auto-before-transient-link"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = upserted.enrollment_items[0].enrollment_item_id
        request = LinkCouponBatchArgs(
            enrollment_item_ids=(item_id,),
            coupon_batch_id="coupon-1",
            tier_mapping={item_id: "base"},
            idempotency_key="transient-link",
        )
        original = harness.workflow_repository.link_coupon_batch

        async def fail_once(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise BusinessRepositoryError("injected business write failure")

        monkeypatch.setattr(harness.workflow_repository, "link_coupon_batch", fail_once)
        with pytest.raises(BusinessRepositoryError, match="injected"):
            await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]
        async with harness.databases.business_sessions() as session:
            failed_counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT status FROM tool_executions WHERE "
                        "tool_name = 'link_coupon_batch'), "
                        "(SELECT COUNT(*) FROM enrollment_coupon_links), "
                        "(SELECT COUNT(*) FROM domain_events WHERE "
                        "event_type = 'enrollment.coupon_batch_linked'), "
                        "(SELECT COUNT(*) FROM audit_events WHERE "
                        "action = 'link_coupon_batch'), "
                        "(SELECT COUNT(*) FROM outbox WHERE "
                        "topic = 'enrollment.coupon_batch_linked')"
                    )
                )
            ).one()

        monkeypatch.setattr(harness.workflow_repository, "link_coupon_batch", original)
        recovered = await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]

    assert failed_counts == ("reserved", 0, 0, 0, 0)
    assert len(recovered.links) == 1
