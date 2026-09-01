"""Cross-database enrollment-version approval invalidation recovery tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.test_v03_enrollment_branches import (
    coordinator,
    event,
    persisted_wait,
)
from tests.support.enrollment import NOW, enrollment_harness

from oria.core.approvals import (
    Approval,
    ApprovalBindingInvalidationConsumer,
    ApprovalBindingInvalidationFact,
    ApprovalBusinessBinding,
    ApprovalResumeRequest,
    ApprovalService,
)
from oria.core.types import RetryPolicy, ToolPolicy
from oria.domain.enrollment_branch import EnrollmentBranchState
from oria.storage.platform import (
    SQLiteApprovalInvalidationRepository,
    SQLiteApprovalRepository,
)

pytestmark = pytest.mark.integration

HASH = "sha256:" + "a" * 64


class ProcessExitInvalidator:
    async def consume(self, fact: ApprovalBindingInvalidationFact) -> object:
        del fact
        raise SystemExit("synthetic process exit after Business commit")


def _approved(binding: ApprovalBusinessBinding) -> Approval:
    return Approval(
        approval_id="approval-stale",
        tenant_id="local-community",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-stale",
        policy_version="local-v1",
        expires_at=NOW + timedelta(days=30),
        status="approved",
        requester="requester",
        decider="approver",
        decision="approve",
        created_at=NOW,
        updated_at=NOW,
        decided_at=NOW,
        business_binding=binding,
    )


def _tool_policy() -> ToolPolicy:
    return ToolPolicy(
        risk_level="high",
        side_effect=True,
        timeout_seconds=30,
        retry_policy=RetryPolicy(max_attempts=1),
        idempotency_scope="launch-plan",
        required_action="campaign:launch:request",
        resource_type="campaign",
        approval_mode="required",
        approval_action="launch_approval",
    )


async def _seed_and_close(harness: object, invalidator: object):
    branch = coordinator(harness, invalidator=invalidator)  # type: ignore[arg-type]
    state = EnrollmentBranchState.from_snapshot(
        campaign_id="campaign-1",
        snapshot=harness.snapshot,  # type: ignore[attr-defined]
    )
    accepted = await branch.process_event(
        state,
        event("accepted-before-close"),
        wait=await persisted_wait(harness),
        ctx=harness.ctx,  # type: ignore[attr-defined,arg-type]
    )
    return branch, accepted.state.model_copy(update={"window_closed": True})


async def _outbox_fact(harness: object) -> ApprovalBindingInvalidationFact:
    async with harness.databases.business_sessions() as session:  # type: ignore[attr-defined]
        row = (
            (
                await session.execute(
                    text(
                        "SELECT event_id, tenant_id, payload_json, occurred_at FROM outbox WHERE "
                        "topic = 'enrollment.version_created'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return ApprovalBindingInvalidationFact.from_outbox(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        payload_json=str(row["payload_json"]),
        occurred_at=row["occurred_at"],
    )


@pytest.mark.asyncio
async def test_invalidator_failure_enters_reconciliation_and_resume_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with enrollment_harness(
        tmp_path,
        mode="merchant",
        late_event_action="new_version",
    ) as harness:
        invalidations = SQLiteApprovalInvalidationRepository(harness.databases.platform_sessions)
        consumer = ApprovalBindingInvalidationConsumer(invalidations)
        branch, closed = await _seed_and_close(harness, consumer)
        binding_v1 = await harness.workflow_repository.get_approval_binding(
            tenant_id="local-community",
            campaign_id="campaign-1",
        )
        assert binding_v1 is not None
        approvals = SQLiteApprovalRepository(harness.databases.platform_sessions)
        await approvals.add(_approved(binding_v1))

        async def fail_apply(*args: object, **kwargs: object) -> int:
            del args, kwargs
            raise RuntimeError("synthetic invalidator failure")

        monkeypatch.setattr(invalidations, "_invalidate", fail_apply)
        outcome = await branch.process_event(
            closed,
            event("late-after-close"),
            wait=None,
            ctx=harness.ctx,  # type: ignore[arg-type]
        )
        fact = await _outbox_fact(harness)
        async with harness.databases.platform_sessions() as session:
            invalidation_status = await session.scalar(
                text(
                    "SELECT status FROM approval_binding_invalidations WHERE event_id = :event_id"
                ),
                {"event_id": fact.event_id},
            )
        approval_service = ApprovalService(
            approvals,
            harness.policy,
            clock=lambda: NOW + timedelta(days=1),
            binding_reader=harness.workflow_repository,
        )
        with pytest.raises(PermissionError, match="business binding is stale"):
            await approval_service.authorize_resume(
                request=ApprovalResumeRequest(
                    approval_id="approval-stale",
                    approval_action="launch_approval",
                    tool_name="LaunchPlan",
                    canonical_args_hash=HASH,
                    checkpoint_id="checkpoint-stale",
                    approval_required=True,
                    business_binding=binding_v1,
                ),
                tool_policy=_tool_policy(),
                ctx=harness.ctx,  # type: ignore[arg-type]
            )

    assert outcome.state.downstream_approval_invalidated is False
    assert outcome.state.downstream_approval_invalidation_pending is True
    assert invalidation_status == "reconciliation"


@pytest.mark.asyncio
async def test_process_exit_leaves_outbox_and_repeated_consume_is_idempotent(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(
        tmp_path,
        mode="merchant",
        late_event_action="new_version",
    ) as harness:
        branch, closed = await _seed_and_close(harness, ProcessExitInvalidator())
        binding_v1 = await harness.workflow_repository.get_approval_binding(
            tenant_id="local-community",
            campaign_id="campaign-1",
        )
        assert binding_v1 is not None
        approvals = SQLiteApprovalRepository(harness.databases.platform_sessions)
        await approvals.add(_approved(binding_v1))
        with pytest.raises(SystemExit, match="synthetic process exit"):
            await branch.process_event(
                closed,
                event("late-process-exit"),
                wait=None,
                ctx=harness.ctx,  # type: ignore[arg-type]
            )
        fact = await _outbox_fact(harness)
        consumer = ApprovalBindingInvalidationConsumer(
            SQLiteApprovalInvalidationRepository(harness.databases.platform_sessions)
        )
        first = await consumer.consume(fact)
        repeated = await consumer.consume(fact)
        persisted = await approvals.get("local-community", "approval-stale")
        binding_v2 = await harness.workflow_repository.get_approval_binding(
            tenant_id="local-community",
            campaign_id="campaign-1",
        )

    assert binding_v2 is not None and binding_v2.enrollment_version == 2
    assert first.status == repeated.status == "applied"
    assert first.invalidated_count == 1
    assert repeated.invalidated_count == 0
    assert persisted is not None and persisted.status == "invalidated"
