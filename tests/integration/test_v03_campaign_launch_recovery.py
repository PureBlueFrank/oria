"""Recovery semantics for partial launch success, unknown outcomes, and compensation."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.launch import NOW, launch_harness

from oria.domain.business import LaunchSagaState

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


@pytest.mark.asyncio
async def test_publish_failure_keeps_materialized_coupon_and_requires_reconciliation(
    tmp_path: Path,
) -> None:
    async with launch_harness(tmp_path, recruitment_status="rejected") as harness:
        result = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            coupon_status = await session.scalar(
                text("SELECT status FROM coupon_batches WHERE coupon_batch_id = 'coupon-1'")
            )
            executions = (
                await session.execute(
                    text("SELECT tool_name, status FROM tool_executions ORDER BY tool_name")
                )
            ).all()

    assert result.saga.status == "reconciliation_required"
    assert coupon_status == "ready"
    assert executions == [
        ("materialize_coupon_batch", "succeeded"),
        ("publish_recruitment", "failed"),
    ]
    assert harness.coupon_adapter.compensation_calls == []


@pytest.mark.asyncio
async def test_unknown_publish_enters_reconciliation_and_is_never_blindly_retried(
    tmp_path: Path,
) -> None:
    async with launch_harness(tmp_path, recruitment_status="unknown") as harness:
        first = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        repeated = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            execution = (
                await session.execute(
                    text(
                        "SELECT status, compensation_status FROM tool_executions "
                        "WHERE tool_name = 'publish_recruitment'"
                    )
                )
            ).one()
            coupon_status = await session.scalar(
                text("SELECT status FROM coupon_batches WHERE coupon_batch_id = 'coupon-1'")
            )

    assert first.saga.status == repeated.saga.status == "reconciliation_required"
    assert execution == ("unknown", "reconciliation_required")
    assert coupon_status == "ready"
    assert len(harness.recruitment_adapter.publish_calls) == 1


@pytest.mark.asyncio
async def test_saga_resumes_from_unadvanced_checkpoint_using_child_ledger_history(
    tmp_path: Path,
) -> None:
    async with launch_harness(tmp_path) as harness:
        await harness.launch_repository.create_saga(
            LaunchSagaState(
                launch_saga_id="launch-saga-crash",
                tenant_id="local-community",
                campaign_id="campaign-1",
                status="planned",
                checkpoint=harness.request.checkpoint_id,
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        materialized = await harness.service.materialize_coupon_batch(
            args=harness.request.materialize_args,
            plan=harness.request.plan,
            approval_id=harness.request.approval_id,
            checkpoint_id=harness.request.checkpoint_id,
            ctx=harness.admin_ctx,  # type: ignore[arg-type]
        )
        resumed = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )

    assert materialized.status == "succeeded"
    assert resumed.saga.status == "completed"
    assert len(harness.coupon_adapter.materialize_calls) == 1
    assert len(harness.recruitment_adapter.publish_calls) == 1


@pytest.mark.asyncio
async def test_only_verified_idempotent_compensation_runs_and_replays_history(
    tmp_path: Path,
) -> None:
    async with launch_harness(
        tmp_path,
        recruitment_status="rejected",
        compensation_contract_verified=True,
        verified_compensation_policy=True,
    ) as harness:
        first = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        repeated = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            coupon_status = await session.scalar(
                text("SELECT status FROM coupon_batches WHERE coupon_batch_id = 'coupon-1'")
            )
            compensation = (
                await session.execute(
                    text(
                        "SELECT status, idempotency_key FROM tool_executions "
                        "WHERE tool_name = 'compensate_coupon_batch'"
                    )
                )
            ).one()

    assert first.saga.status == repeated.saga.status == "compensation_pending"
    assert first.compensation_execution is not None
    assert repeated.compensation_execution is not None
    assert compensation[0] == "succeeded"
    assert ":compensation:compensation-v1:sha256:" in compensation[1]
    assert coupon_status == "ready"
    assert len(harness.coupon_adapter.compensation_calls) == 1
    assert len(harness.recruitment_adapter.publish_calls) == 1
