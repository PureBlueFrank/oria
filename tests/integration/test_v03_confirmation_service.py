"""Transactional V0.3-T05 business confirmation chain integration tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from tests.support.enrollment import EXECUTOR, NOW, auto_command, enrollment_harness

from oria.core.execution_ledger import ExecutionLedger
from oria.core.types import Principal
from oria.domain.confirmations import ConfirmationService
from oria.domain.enrollment import EnrollmentItemInput, LinkCouponBatchArgs
from oria.permission.local import LocalPolicyEngine
from oria.storage.repositories import BusinessRepositoryError

pytestmark = pytest.mark.integration


def _actor(subject_id: str, role: str) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id="local-community",
        kind="human" if role != "confirmation_automation" else "service",
        roles=(role,),
        authn_method="trusted-test-profile",
    )


MERCHANT = _actor("demo-m001", "merchant")
SALES = _actor("sales-1", "sales")
MANAGER = _actor("manager-1", "sales_manager")
AUTOMATION = _actor("confirmation-automation", "confirmation_automation")


def _ctx(actor: Principal, policy: LocalPolicyEngine, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        actor=actor,
        executor=EXECUTOR,
        tenant_id=actor.tenant_id,
        correlation_id=f"confirmation-{run_id}",
        run_id=run_id,
        policy=policy,
    )


async def _enroll(harness: object, circle_run_id: str = "confirmation-circle"):
    items = (
        EnrollmentItemInput(
            merchant_id="demo-m001",
            product_ref="product-1",
            product_version="v1",
        ),
    )
    return await harness.enrollments.upsert_auto(  # type: ignore[attr-defined]
        auto_command(items, circle_run_id=circle_run_id),
        harness.ctx,  # type: ignore[attr-defined,arg-type]
    )


def _service(harness: object, *, now=NOW) -> ConfirmationService:
    return ConfirmationService(
        repository=harness.workflow_repository,  # type: ignore[attr-defined]
        ledger=ExecutionLedger(harness.databases.business_sessions, clock=lambda: now),  # type: ignore[attr-defined]
        clock=lambda: now,
    )


def _policy(tasks: tuple[object, ...]) -> LocalPolicyEngine:
    assignments = {
        task.confirmation_task_id: task.subject_id  # type: ignore[attr-defined]
        for task in tasks
    }
    return LocalPolicyEngine(
        trusted_actors=(MERCHANT, SALES, MANAGER, AUTOMATION),
        trusted_executors=(EXECUTOR,),
        confirmation_assignments=assignments,
    )


@pytest.mark.asyncio
async def test_confirmation_chain_rejects_wrong_subject_sequence_and_repeat_then_allows_link(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        enrolled = await _enroll(harness)
        item_id = enrolled.enrollment_items[0].enrollment_item_id
        merchant_task, sales_task, manager_task = enrolled.confirmation_tasks
        policy = _policy(enrolled.confirmation_tasks)
        service = _service(harness)

        with pytest.raises(PermissionError, match="not authorized"):
            await service.decide(
                merchant_task.confirmation_task_id,
                "confirm",
                _ctx(SALES, policy, "wrong-subject"),  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="sequence is not active"):
            await service.decide(
                manager_task.confirmation_task_id,
                "confirm",
                _ctx(MANAGER, policy, "out-of-order"),  # type: ignore[arg-type]
            )
        with pytest.raises(BusinessRepositoryError, match="incomplete"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(item_id,),
                    coupon_batch_id="coupon-1",
                    tier_mapping={item_id: "base"},
                    idempotency_key="before-confirmation",
                ),
                harness.ctx,  # type: ignore[arg-type]
            )

        first = await service.decide(
            merchant_task.confirmation_task_id,
            "confirm",
            _ctx(MERCHANT, policy, "merchant"),  # type: ignore[arg-type]
        )
        assert first.next_confirmation_task is not None
        assert first.next_confirmation_task.confirmation_task_id == sales_task.confirmation_task_id
        with pytest.raises(ValueError, match="already resolved"):
            await service.decide(
                merchant_task.confirmation_task_id,
                "confirm",
                _ctx(MERCHANT, policy, "merchant-repeat"),  # type: ignore[arg-type]
            )
        await service.decide(
            sales_task.confirmation_task_id,
            "confirm",
            _ctx(SALES, policy, "sales"),  # type: ignore[arg-type]
        )
        completed = await service.decide(
            manager_task.confirmation_task_id,
            "confirm",
            _ctx(MANAGER, policy, "manager"),  # type: ignore[arg-type]
        )
        linked = await harness.links.link(
            LinkCouponBatchArgs(
                enrollment_item_ids=(item_id,),
                coupon_batch_id="coupon-1",
                tier_mapping={item_id: "base"},
                idempotency_key="after-confirmation",
            ),
            harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            event_counts = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM domain_events WHERE event_type = "
                        "'confirmation.decided'), "
                        "(SELECT COUNT(*) FROM audit_events WHERE action = "
                        "'decide_confirmation'), "
                        "(SELECT COUNT(*) FROM outbox WHERE topic = 'confirmation.decided')"
                    )
                )
            ).one()

    assert completed.enrollment_item.status == "confirmed"
    assert completed.next_confirmation_task is None
    assert len(linked.links) == 1
    assert event_counts == (3, 3, 3)


@pytest.mark.asyncio
async def test_rejection_is_terminal_and_cancels_later_steps(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path) as harness:
        enrolled = await _enroll(harness, "rejection-circle")
        policy = _policy(enrolled.confirmation_tasks)
        rejected = await _service(harness).decide(
            enrolled.confirmation_tasks[0].confirmation_task_id,
            "reject",
            _ctx(MERCHANT, policy, "reject"),  # type: ignore[arg-type]
        )

    assert rejected.enrollment_item.status == "rejected"
    assert tuple(task.status for task in rejected.confirmation_tasks) == (
        "rejected",
        "cancelled",
        "cancelled",
    )


@pytest.mark.asyncio
async def test_confirmation_mutation_and_terminal_facts_roll_back_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        enrolled = await _enroll(harness, "rollback-circle")
        policy = _policy(enrolled.confirmation_tasks)
        original = harness.workflow_repository.apply_confirmation_chain

        async def fail_after_mutation(*args: object, **kwargs: object) -> None:
            await original(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("synthetic confirmation transaction failure")

        monkeypatch.setattr(
            harness.workflow_repository,
            "apply_confirmation_chain",
            fail_after_mutation,
        )
        with pytest.raises(RuntimeError, match="synthetic confirmation"):
            await _service(harness).decide(
                enrolled.confirmation_tasks[0].confirmation_task_id,
                "confirm",
                _ctx(MERCHANT, policy, "rollback"),  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            task_statuses = tuple(
                row[0]
                for row in await session.execute(
                    text("SELECT status FROM confirmation_tasks ORDER BY sequence")
                )
            )
            item_status = await session.scalar(text("SELECT status FROM enrollment_items"))
            terminal_counts = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM tool_executions WHERE tool_name = "
                        "'decide_confirmation' AND status = 'succeeded'), "
                        "(SELECT COUNT(*) FROM domain_events WHERE event_type = "
                        "'confirmation.decided'), "
                        "(SELECT COUNT(*) FROM audit_events WHERE action = "
                        "'decide_confirmation'), "
                        "(SELECT COUNT(*) FROM outbox WHERE topic = 'confirmation.decided')"
                    )
                )
            ).one()

    assert task_statuses == ("pending", "waiting", "waiting")
    assert item_status == "pending_confirmation"
    assert terminal_counts == (0, 0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_action", "steps", "expected_item", "expected_tasks"),
    [
        ("reject", ("merchant",), "rejected", ("rejected",)),
        ("escalate", ("merchant", "sales"), "pending_confirmation", ("timed_out", "pending")),
        ("explicit_auto_confirm", ("merchant",), "confirmed", ("confirmed",)),
    ],
)
async def test_timeout_actions_persist_reject_escalate_or_explicit_auto_confirm(
    tmp_path: Path,
    timeout_action: str,
    steps: tuple[str, ...],
    expected_item: str,
    expected_tasks: tuple[str, ...],
) -> None:
    async with enrollment_harness(
        tmp_path,
        confirmation_steps=steps,  # type: ignore[arg-type]
        confirmation_timeout_action=timeout_action,  # type: ignore[arg-type]
    ) as harness:
        enrolled = await _enroll(harness, f"timeout-{timeout_action}")
        policy = _policy(enrolled.confirmation_tasks)
        service = _service(harness, now=NOW + timedelta(days=2))
        if timeout_action == "explicit_auto_confirm":
            with pytest.raises(PermissionError, match="not authorized"):
                await service.resolve_timeout(
                    enrolled.confirmation_tasks[0].confirmation_task_id,
                    _ctx(MERCHANT, policy, "auto-denied"),  # type: ignore[arg-type]
                )
        resolved = await service.resolve_timeout(
            enrolled.confirmation_tasks[0].confirmation_task_id,
            _ctx(AUTOMATION, policy, f"timeout-{timeout_action}"),  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            persisted = await session.execute(
                text(
                    "SELECT status FROM confirmation_tasks WHERE tenant_id = "
                    "'local-community' ORDER BY sequence"
                )
            )

    assert resolved.enrollment_item.status == expected_item
    assert tuple(row[0] for row in persisted) == expected_tasks
