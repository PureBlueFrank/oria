"""V0.3-T05 merchant/auto/hybrid event-window and join integration tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import NOW, auto_command, enrollment_harness

from oria.core.approvals import ApprovalBindingInvalidationConsumer
from oria.core.integration_events import (
    ExternalWait,
    IntegrationEventInboxService,
)
from oria.domain.enrollment import EnrollmentItemInput
from oria.domain.enrollment_branch import (
    DownstreamApprovalInvalidator,
    EnrollmentBranchCoordinator,
    EnrollmentBranchState,
)
from oria.storage.platform import (
    SQLiteApprovalInvalidationRepository,
    SQLiteIntegrationEventInboxRepository,
)

pytestmark = pytest.mark.integration


def event(
    source_event_id: str, event_type: str = "merchant.enrollment_upserted"
) -> dict[str, object]:
    payload: dict[str, object]
    if event_type == "enrollment.window_closed":
        payload = {"campaign_id": "campaign-1", "enrollment_window_ref": "window-v1"}
    else:
        payload = {
            "campaign_id": "campaign-1",
            "enrollment_id": "caller-id-is-not-trusted",
            "merchant_id": "demo-m001",
            "product_ref": "product-1",
            "product_version": "v1",
        }
    return {
        "schema_version": 1,
        "event_type": event_type,
        "tenant_id": "local-community",
        "adapter_id": "merchant-adapter",
        "source_event_id": source_event_id,
        "signature_subject": "adapter-principal",
        "version": 1,
        "payload": payload,
    }


def wait(event_type: str = "merchant.enrollment_upserted") -> ExternalWait:
    return ExternalWait(
        tenant_id="local-community",
        wait_id=f"trusted-wait-{event_type}",
        event_type=event_type,  # type: ignore[arg-type]
        resource_type="campaign",
        resource_id="campaign-1",
        expected_version=1,
        checkpoint_id="trusted-checkpoint",
        expires_at=NOW + timedelta(days=60),
        timeout_action="queue",
        status="waiting",
        created_at=NOW - timedelta(days=1),
    )


async def persisted_wait(harness: object, event_type: str = "merchant.enrollment_upserted"):
    value = wait(event_type)
    repository = SQLiteIntegrationEventInboxRepository(
        harness.databases.platform_sessions  # type: ignore[attr-defined]
    )
    await repository.add_wait(value)
    return value


def coordinator(
    harness: object,
    *,
    clock: Callable[[], datetime] | None = None,
    invalidator: DownstreamApprovalInvalidator | None = None,
):
    platform_sessions = harness.databases.platform_sessions  # type: ignore[attr-defined]
    return EnrollmentBranchCoordinator(
        inbox=IntegrationEventInboxService(
            SQLiteIntegrationEventInboxRepository(platform_sessions),
            authorized_subjects={
                ("local-community", "merchant-adapter"): frozenset({"adapter-principal"})
            },
            clock=lambda: NOW,
        ),
        enrollments=harness.enrollments,  # type: ignore[attr-defined]
        approval_invalidator=invalidator
        or ApprovalBindingInvalidationConsumer(
            SQLiteApprovalInvalidationRepository(platform_sessions)
        ),
        clock=clock or (lambda: NOW),
    )


@pytest.mark.asyncio
async def test_merchant_mode_waits_for_window_close_before_join(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, mode="merchant") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        branch = coordinator(harness)
        merchant_wait = await persisted_wait(harness)
        closed_wait = await persisted_wait(harness, "enrollment.window_closed")

        accepted = await branch.process_event(
            state,
            event("merchant-1"),
            wait=merchant_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        closed = await branch.process_event(
            accepted.state,
            event("closed-1", "enrollment.window_closed"),
            wait=closed_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.platform_sessions() as session:
            persisted = tuple(
                await session.scalars(
                    text(
                        "SELECT source_event_id FROM integration_event_inbox "
                        "ORDER BY source_event_id"
                    )
                )
            )

    assert accepted.status == "accepted" and accepted.state.join_complete is False
    assert closed.status == "window_closed" and closed.state.join_complete is True
    assert persisted == ("closed-1", "merchant-1")


@pytest.mark.asyncio
async def test_auto_mode_finishes_immediately_after_deterministic_circle(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, mode="auto") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        branch = coordinator(harness)

        completed = await branch.complete_auto(
            state,
            items := (
                EnrollmentItemInput(
                    merchant_id="demo-m001",
                    product_ref="product-1",
                    product_version="v1",
                ),
            ),
            binding=auto_command(items, circle_run_id="auto-circle-v1").binding,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )

    assert completed.status == "auto_completed"
    assert completed.state.join_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_updates", "clock_offset", "message"),
    [
        ({}, "before", "has not opened"),
        ({"window_closed": True}, "inside", "is closed"),
        ({}, "after", "is closed"),
        ({"auto_completed": True}, "inside", "already complete"),
    ],
)
async def test_auto_branch_rejects_illegal_window_and_completion_states(
    tmp_path: Path,
    state_updates: dict[str, object],
    clock_offset: str,
    message: str,
) -> None:
    async with enrollment_harness(tmp_path, mode="auto") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        ).model_copy(update=state_updates)
        moments = {
            "before": state.enrollment_window_start - timedelta(seconds=1),
            "inside": NOW,
            "after": state.enrollment_window_end,
        }
        branch = coordinator(harness, clock=lambda: moments[clock_offset])
        items = (
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        )

        with pytest.raises(ValueError, match=message):
            await branch.complete_auto(
                state,
                items,
                binding=auto_command(items, circle_run_id=f"illegal-{clock_offset}").binding,
                ctx=harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_items"))

    assert count == 0


@pytest.mark.asyncio
async def test_hybrid_waits_for_both_branches_and_deduplicates_the_business_key(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, mode="hybrid") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        branch = coordinator(harness)
        merchant_wait = await persisted_wait(harness)
        closed_wait = await persisted_wait(harness, "enrollment.window_closed")
        auto = await branch.complete_auto(
            state,
            items := (
                EnrollmentItemInput(
                    merchant_id="demo-m001",
                    product_ref="product-1",
                    product_version="v1",
                ),
            ),
            binding=auto_command(items, circle_run_id="auto-circle-v1").binding,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        merchant = await branch.process_event(
            auto.state,
            event("merchant-1"),
            wait=merchant_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        duplicate = await branch.process_event(
            merchant.state,
            event("merchant-1"),
            wait=merchant_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        closed = await branch.process_event(
            merchant.state,
            event("closed-1", "enrollment.window_closed"),
            wait=closed_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            row = (
                await session.execute(text("SELECT sources_json, COUNT(*) FROM enrollment_items"))
            ).one()
        async with harness.databases.platform_sessions() as session:
            inbox_count = await session.scalar(text("SELECT COUNT(*) FROM integration_event_inbox"))

    assert auto.state.join_complete is False
    assert merchant.state.join_complete is False
    assert duplicate.status == "duplicate"
    assert closed.state.join_complete is True
    assert row == ('["auto","merchant"]', 1)
    assert inbox_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("late_action", ["reject", "new_version"])
async def test_closed_window_late_event_is_rejected_or_creates_a_new_version(
    tmp_path: Path,
    late_action: str,
) -> None:
    async with enrollment_harness(
        tmp_path,
        mode="merchant",
        late_event_action=late_action,  # type: ignore[arg-type]
    ) as harness:
        branch = coordinator(harness)
        merchant_wait = await persisted_wait(harness)
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        accepted = await branch.process_event(
            state,
            event("accepted-1"),
            wait=merchant_wait,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        state = accepted.state.model_copy(update={"window_closed": True})

        outcome = await branch.process_event(
            state,
            event("late-1"),
            wait=None,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            enrollment_count = await session.scalar(text("SELECT COUNT(*) FROM enrollments"))
            enrollment_version = await session.scalar(
                text("SELECT version FROM enrollments LIMIT 1")
            )
        async with harness.databases.platform_sessions() as session:
            invalidation = (
                await session.execute(
                    text(
                        "SELECT campaign_id, enrollment_version, status FROM "
                        "approval_binding_invalidations"
                    )
                )
            ).one_or_none()

    if late_action == "reject":
        assert outcome.status == "late_rejected"
        assert outcome.state.late_rejected_event_ids == ("late-1",)
        assert enrollment_count == 1 and enrollment_version == 1
        assert invalidation is None
    else:
        assert outcome.status == "new_version"
        assert outcome.state.enrollment_version == 2
        assert outcome.state.downstream_approval_invalidated is True
        assert enrollment_count == 1 and enrollment_version == 2
        assert invalidation == ("campaign-1", 2, "applied")
