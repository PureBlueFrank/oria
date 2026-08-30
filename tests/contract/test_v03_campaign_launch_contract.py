"""Contract coverage for approved launch child tools and their independent ledgers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.launch import NOW, launch_harness

from oria.domain.business import LaunchSagaState

pytestmark = pytest.mark.contract


@pytest.mark.asyncio
async def test_materialize_and_publish_use_independent_ledgers_and_replay_history_once(
    tmp_path: Path,
) -> None:
    async with launch_harness(tmp_path) as harness:
        first = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        repeated = await harness.service.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM tool_executions), "
                        "(SELECT COUNT(*) FROM domain_events), "
                        "(SELECT COUNT(*) FROM audit_events), "
                        "(SELECT COUNT(*) FROM outbox)"
                    )
                )
            ).one()
            coupon_status = await session.scalar(
                text("SELECT status FROM coupon_batches WHERE coupon_batch_id = 'coupon-1'")
            )
            publication = (
                await session.execute(
                    text(
                        "SELECT status, receipt_id FROM recruitment_publications "
                        "WHERE recruitment_publication_id = 'publication-1'"
                    )
                )
            ).one()

    assert first.saga.status == repeated.saga.status == "completed"
    assert counts == (2, 2, 2, 2)
    assert coupon_status == "ready"
    assert publication[0] == "published" and publication[1]
    assert len(harness.coupon_adapter.materialize_calls) == 1
    assert len(harness.recruitment_adapter.publish_calls) == 1
    assert harness.coupon_adapter.materialize_calls[0].startswith(
        f"campaign-1:{harness.draft.coupon_batch.coupon_spec_hash}:sha256:"
    )


@pytest.mark.asyncio
async def test_success_business_write_ledger_and_events_roll_back_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with launch_harness(tmp_path) as harness:
        await harness.launch_repository.create_saga(
            LaunchSagaState(
                launch_saga_id="launch-saga-rollback",
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
        original = harness.launch_repository.mark_coupon_ready

        async def fail_after_business_write(
            session: AsyncSession,
            *,
            tenant_id: str,
            coupon_batch_id: str,
            updated_at: datetime,
        ) -> None:
            await original(
                session,
                tenant_id=tenant_id,
                coupon_batch_id=coupon_batch_id,
                updated_at=updated_at,
            )
            raise RuntimeError("injected transaction failure")

        monkeypatch.setattr(
            harness.launch_repository,
            "mark_coupon_ready",
            fail_after_business_write,
        )
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            await harness.service.materialize_coupon_batch(
                args=harness.request.materialize_args,
                plan=harness.request.plan,
                approval_id=harness.request.approval_id,
                checkpoint_id=harness.request.checkpoint_id,
                ctx=harness.admin_ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, "
                        "(SELECT status FROM coupon_batches WHERE coupon_batch_id = 'coupon-1') "
                        "FROM tool_executions WHERE tool_name = 'materialize_coupon_batch'"
                    )
                )
            ).one()
            facts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM domain_events), "
                        "(SELECT COUNT(*) FROM audit_events), (SELECT COUNT(*) FROM outbox)"
                    )
                )
            ).one()

    assert row == ("executing", "draft")
    assert facts == (0, 0, 0)
