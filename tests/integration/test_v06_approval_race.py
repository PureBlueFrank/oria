"""A concurrent approval loser must not overwrite a decision or its audit trail."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from tests.security.test_v03_approval_rbac import EXECUTOR, _policy, _principal

from oria.config import resolve_runtime_config
from oria.core.approvals import Approval, ApprovalService
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.mark.asyncio
async def test_concurrent_decisions_have_one_winner_and_one_audit(tmp_path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    now = datetime.now(UTC)
    async with DatabaseResources(config) as db:
        repository = SQLiteApprovalRepository(db.platform_sessions)
        await repository.add(
            Approval(
                approval_id="race",
                tenant_id="tenant-a",
                approval_action="launch_approval",
                tool_name="LaunchPlan",
                canonical_args_hash="sha256:" + "a" * 64,
                checkpoint_id="checkpoint",
                policy_version="local-v2",
                requester="requester",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        actors = [
            _principal("approver-1", "launch_approver"),
            _principal("approver-2", "launch_approver"),
        ]
        barrier = asyncio.Event()
        calls = 0
        delegate = _policy(*actors)

        class BarrierPolicy:
            async def authorize(self, request, ctx):
                nonlocal calls
                calls += 1
                if calls == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 5)
                return await delegate.authorize(request, ctx)

        policy = BarrierPolicy()
        service = ApprovalService(repository, policy)
        results = await asyncio.gather(
            *(
                service.decide(
                    tenant_id="tenant-a",
                    approval_id="race",
                    decision=decision,
                    reason="synthetic decision",
                    ctx=SimpleNamespace(
                        actor=actor,
                        executor=EXECUTOR,
                        tenant_id="tenant-a",
                        correlation_id=actor.subject_id,
                    ),
                )
                for actor, decision in zip(actors, ["approve", "reject"], strict=True)
            ),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, Approval)]
        assert len(winners) == 1, "both racing requests committed a decision"
        assert await repository.get("tenant-a", "race") == winners[0]
        async with db.platform_sessions() as session:
            assert (
                await session.execute(text("SELECT count(*) FROM audit_events"))
            ).scalar_one() == 1
            assert (await session.execute(text("SELECT count(*) FROM outbox"))).scalar_one() == 1
