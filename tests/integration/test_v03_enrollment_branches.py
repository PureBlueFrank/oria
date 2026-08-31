"""V0.3-T05 merchant/auto/hybrid event-window and join integration tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import NOW, auto_command, enrollment_harness

from oria.core.integration_events import (
    ExternalWait,
    IntegrationEventInboxService,
    IntegrationInboxRecord,
)
from oria.domain.enrollment import EnrollmentItemInput
from oria.domain.enrollment_branch import (
    EnrollmentBranchCoordinator,
    EnrollmentBranchState,
    InMemoryDownstreamApprovalInvalidator,
)

pytestmark = pytest.mark.integration


class InboxRepository:
    def __init__(self) -> None:
        self.records: list[IntegrationInboxRecord] = []
        self.get_calls: list[tuple[str, str, str]] = []

    async def add(self, record: IntegrationInboxRecord) -> bool:
        key = (record.tenant_id, record.adapter_id, record.source_event_id)
        if any(
            (item.tenant_id, item.adapter_id, item.source_event_id) == key for item in self.records
        ):
            return False
        self.records.append(record)
        return True

    async def get(
        self,
        tenant_id: str,
        adapter_id: str,
        source_event_id: str,
    ) -> IntegrationInboxRecord | None:
        self.get_calls.append((tenant_id, adapter_id, source_event_id))
        return next(
            (
                record
                for record in self.records
                if (
                    record.tenant_id,
                    record.adapter_id,
                    record.source_event_id,
                )
                == (tenant_id, adapter_id, source_event_id)
            ),
            None,
        )


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


def coordinator(harness: object, repository: InboxRepository, invalidator: object):
    return EnrollmentBranchCoordinator(
        inbox=IntegrationEventInboxService(
            repository,
            authorized_subjects={
                ("local-community", "merchant-adapter"): frozenset({"adapter-principal"})
            },
            clock=lambda: NOW,
        ),
        enrollments=harness.enrollments,  # type: ignore[attr-defined]
        approval_invalidator=invalidator,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_merchant_mode_waits_for_window_close_before_join(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, mode="merchant") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        repository = InboxRepository()
        branch = coordinator(harness, repository, InMemoryDownstreamApprovalInvalidator())

        accepted = await branch.process_event(
            state,
            event("merchant-1"),
            wait=wait(),
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        closed = await branch.process_event(
            accepted.state,
            event("closed-1", "enrollment.window_closed"),
            wait=wait("enrollment.window_closed"),
            ctx=harness.ctx,  # type: ignore[arg-type]
        )

    assert accepted.status == "accepted" and accepted.state.join_complete is False
    assert closed.status == "window_closed" and closed.state.join_complete is True
    assert repository.get_calls == [
        ("local-community", "merchant-adapter", "merchant-1"),
        ("local-community", "merchant-adapter", "closed-1"),
    ]


@pytest.mark.asyncio
async def test_auto_mode_finishes_immediately_after_deterministic_circle(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, mode="auto") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        branch = coordinator(harness, InboxRepository(), InMemoryDownstreamApprovalInvalidator())

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
async def test_hybrid_waits_for_both_branches_and_deduplicates_the_business_key(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, mode="hybrid") as harness:
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        repository = InboxRepository()
        branch = coordinator(harness, repository, InMemoryDownstreamApprovalInvalidator())
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
            wait=wait(),
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        duplicate = await branch.process_event(
            merchant.state,
            event("merchant-1"),
            wait=wait(),
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        closed = await branch.process_event(
            merchant.state,
            event("closed-1", "enrollment.window_closed"),
            wait=wait("enrollment.window_closed"),
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            row = (
                await session.execute(text("SELECT sources_json, COUNT(*) FROM enrollment_items"))
            ).one()

    assert auto.state.join_complete is False
    assert merchant.state.join_complete is False
    assert duplicate.status == "duplicate"
    assert closed.state.join_complete is True
    assert row == ('["auto","merchant"]', 1)


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
        invalidator = InMemoryDownstreamApprovalInvalidator()
        branch = coordinator(harness, InboxRepository(), invalidator)
        state = EnrollmentBranchState.from_snapshot(
            campaign_id="campaign-1", snapshot=harness.snapshot
        )
        accepted = await branch.process_event(
            state,
            event("accepted-1"),
            wait=wait(),
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

    if late_action == "reject":
        assert outcome.status == "late_rejected"
        assert outcome.state.late_rejected_event_ids == ("late-1",)
        assert enrollment_count == 1 and enrollment_version == 1
        assert invalidator.invalidations == []
    else:
        assert outcome.status == "new_version"
        assert outcome.state.enrollment_version == 2
        assert outcome.state.downstream_approval_invalidated is True
        assert enrollment_count == 1 and enrollment_version == 2
        assert len(invalidator.invalidations) == 1
        fact = invalidator.invalidations[0]
        assert fact.tenant_id == "local-community"
        assert fact.binding.campaign_id == "campaign-1"
        assert fact.binding.enrollment_version == 2
        assert fact.reason == "late_enrollment_new_version"
