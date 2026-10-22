"""Actual temporary SQLite records and controlled Mock calls, never real network."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.integration.test_v06_job_launch_approval import launch as launch
from tests.integration.test_v06_job_leases import leases as leases

from oria.adapters.launch import InMemoryCouponBatchAdapter
from oria.core.approvals import ApprovalService
from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict
from oria.orchestrator.job_coupon_dispatch import JobCouponDispatchService
from oria.storage.database import DatabaseResources

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


@pytest_asyncio.fixture
async def dispatch(launch, monkeypatch):
    c = launch
    binding = await c.service.request_launch_approval(c.token, c.args, c.plan)
    approver = c.s.admin.actor.model_copy(
        update={"subject_id": "independent-coupon-approver", "roles": ("launch_approver",)}
    )
    policy = c.s.runtime.policy
    monkeypatch.setattr(policy, "_trusted_actors", policy._trusted_actors | {approver})
    approval = await ApprovalService(c.approvals, policy, clock=c.s.clock.now).decide(
        tenant_id=c.job.tenant_id,
        approval_id=binding.approval.approval_id,
        decision="approve",
        reason=None,
        ctx=c.s.runtime.new_context(
            actor=approver,
            executor=c.s.worker,
            session_id=c.job.session_id,
            thread_id=c.job.thread_id,
            run_id=c.job.run_id,
        ),
    )
    reservation = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, approval.approval_id
    )
    adapter = InMemoryCouponBatchAdapter(clock=c.s.clock.now)
    service = JobCouponDispatchService(c.worker, c.s.db, adapter)
    return SimpleNamespace(
        c=c, approval=approval, reservation=reservation, adapter=adapter, service=service
    )


async def _execution_state(c):
    execution_id = f"{c.job.job_id}:coupon-materialization:v1"
    async with c.s.db.business_sessions() as session:
        execution = (
            await session.execute(
                text(
                    "SELECT status, attempt_count, compensation_status FROM tool_executions "
                    "WHERE tenant_id = :tenant AND execution_id = :execution"
                ),
                {"tenant": c.job.tenant_id, "execution": execution_id},
            )
        ).one()
        coupon = (
            await session.execute(
                text(
                    "SELECT status, version FROM coupon_batches WHERE tenant_id = :tenant "
                    "AND coupon_batch_id = :coupon"
                ),
                {"tenant": c.job.tenant_id, "coupon": c.args.coupon_batch_id},
            )
        ).one()
        saga = (
            await session.execute(
                text(
                    "SELECT status, version FROM launch_saga_states WHERE tenant_id = :tenant "
                    "AND campaign_id = :campaign"
                ),
                {"tenant": c.job.tenant_id, "campaign": c.args.campaign_id},
            )
        ).one()
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM domain_events WHERE tenant_id = :tenant "
                    "AND event_type LIKE 'coupon_batch.job_dispatch.%'"
                ),
                {"tenant": c.job.tenant_id},
            )
        ).scalar_one()
    return execution, coupon, saga, events


@pytest.mark.asyncio
async def test_dispatch_materializes_coupon_and_saga_atomically(dispatch):
    d = dispatch
    c = d.c
    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "succeeded" and result.attempt_count == 1
    assert result.receipt_id and result.receipt_summary_hash
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]
    async with c.s.db.business_sessions() as session:
        coupon = (await session.execute(text("SELECT status, version FROM coupon_batches"))).one()
        saga = (await session.execute(text("SELECT status, version FROM launch_saga_states"))).one()
    assert coupon == ("ready", 3)
    assert saga == ("coupon_materialized", 2)
    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == c.claimed
    replay = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert replay == result
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]
    assert await _execution_state(c) == (
        ("succeeded", 1, None),
        ("ready", 3),
        ("coupon_materialized", 2),
        2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected", "compensation"),
    [("rejected", "failed", None), ("unknown", "unknown", "reconciliation_required")],
)
async def test_terminal_adapter_receipts_are_never_redispatched(
    dispatch, status, expected, compensation
):
    d = dispatch
    c = d.c
    d.adapter.materialize_status = status

    first = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    second = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)

    assert first == second and first.status == expected
    assert first.compensation_status == compensation
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]
    execution, coupon, saga, events = await _execution_state(c)
    assert execution == (expected, 1, compensation)
    assert coupon == ("draft", 1) and saga == ("planned", 1) and events == 2


@pytest.mark.asyncio
async def test_pending_launch_approval_cannot_admit_coupon_dispatch(launch):
    c = launch
    binding = await c.service.request_launch_approval(c.token, c.args, c.plan)
    adapter = InMemoryCouponBatchAdapter(clock=c.s.clock.now)
    service = JobCouponDispatchService(c.worker, c.s.db, adapter)

    with pytest.raises(PermissionError):
        await service.materialize(c.token, c.args, c.plan, binding.approval.approval_id)

    async with c.s.db.business_sessions() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_executions WHERE tenant_id = :tenant "
                    "AND execution_id = :execution"
                ),
                {
                    "tenant": c.job.tenant_id,
                    "execution": f"{c.job.job_id}:coupon-materialization:v1",
                },
            )
        ).scalar_one()
    assert count == 0 and adapter.materialize_calls == []


@pytest.mark.asyncio
async def test_stale_approval_policy_version_cannot_admit_dispatch(dispatch, monkeypatch):
    d = dispatch
    c = d.c
    original = c.s.runtime.policy.evaluate_now

    def stale_policy(request, ctx):
        return original(request, ctx).model_copy(update={"policy_version": "retired-policy"})

    monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", stale_policy)
    with pytest.raises(PermissionError):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)

    assert (await _execution_state(c))[0] == ("reserved", 0, None)
    assert d.adapter.materialize_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_adapter_errors_and_timeouts_become_redacted_unknown(dispatch, monkeypatch, failure):
    d = dispatch
    c = d.c
    if failure == "exception":

        async def fail(*args, **kwargs):
            raise RuntimeError("secret adapter response")

        monkeypatch.setattr(d.adapter, "materialize", fail)
    else:

        async def hang(*args, **kwargs):
            await asyncio.sleep(0.02)
            return await InMemoryCouponBatchAdapter.materialize(d.adapter, *args, **kwargs)

        d.service._timeout = 0.001
        monkeypatch.setattr(d.adapter, "materialize", hang)

    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "unknown"
    assert result.compensation_status == "reconciliation_required"
    execution, coupon, saga, events = await _execution_state(c)
    assert execution == ("unknown", 1, "reconciliation_required")
    assert coupon == ("draft", 1) and saga == ("planned", 1) and events == 2
    async with c.s.db.business_sessions() as session:
        payloads = (
            (
                await session.execute(
                    text(
                        "SELECT payload_json FROM outbox "
                        "WHERE topic LIKE 'coupon_batch.job_dispatch.%'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "secret adapter response" not in "".join(payloads)


@pytest.mark.asyncio
async def test_cancelled_adapter_call_leaves_executing_then_fails_closed(dispatch, monkeypatch):
    d = dispatch
    c = d.c
    entered = asyncio.Event()
    never = asyncio.Event()
    calls = 0

    async def pause(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await never.wait()

    monkeypatch.setattr(d.adapter, "materialize", pause)
    task = asyncio.create_task(
        d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    execution, coupon, saga, events = await _execution_state(c)
    assert execution == ("executing", 1, None)
    assert coupon == ("draft", 1) and saga == ("planned", 1) and events == 1
    monkeypatch.undo()
    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "unknown"
    assert calls == 1 and d.adapter.materialize_calls == []
    assert (await _execution_state(c))[0] == ("unknown", 1, "reconciliation_required")


@pytest.mark.asyncio
async def test_outcome_commit_failure_keeps_executing_and_replay_never_calls_again(
    dispatch, monkeypatch
):
    d = dispatch
    c = d.c
    original = d.adapter.materialize
    returned = False

    async def record_return(*args, **kwargs):
        nonlocal returned
        receipt = await original(*args, **kwargs)
        returned = True
        return receipt

    monkeypatch.setattr(d.adapter, "materialize", record_return)
    business_engine = c.s.db.business_sessions.kw["bind"].sync_engine

    def fail_outcome_commit(session):
        if returned and session.bind is business_engine:
            raise RuntimeError("synthetic outcome commit failure")

    event.listen(Session, "before_commit", fail_outcome_commit)
    try:
        with pytest.raises(RuntimeError, match="synthetic outcome commit failure"):
            await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    finally:
        event.remove(Session, "before_commit", fail_outcome_commit)

    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]
    assert (await _execution_state(c)) == (("executing", 1, None), ("draft", 1), ("planned", 1), 1)
    monkeypatch.undo()
    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "unknown"
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_expired_lease_after_adapter_call_cannot_commit_or_replay(dispatch, monkeypatch):
    d = dispatch
    c = d.c
    original = d.adapter.materialize

    async def expire_after_receipt(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        c.s.clock.advance(31)
        return receipt

    monkeypatch.setattr(d.adapter, "materialize", expire_after_receipt)
    with pytest.raises((PermissionError, JobConflict)):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert (await _execution_state(c)) == (("executing", 1, None), ("draft", 1), ("planned", 1), 1)
    with pytest.raises((PermissionError, JobConflict)):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)

    claimed = await c.worker.claim(c.job.job_id)
    result = await d.service.materialize(
        lease_token(claimed), c.args, c.plan, d.approval.approval_id
    )
    assert result.status == "unknown"
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_policy_revocation_after_mock_receipt_prevents_projection_commit(
    dispatch, monkeypatch
):
    d = dispatch
    c = d.c
    original_adapter = d.adapter.materialize
    original_policy = c.s.runtime.policy.evaluate_now

    async def revoke_after_receipt(*args, **kwargs):
        receipt = await original_adapter(*args, **kwargs)

        def deny(request, ctx):
            return original_policy(request, ctx).model_copy(update={"allow": False})

        monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", deny)
        return receipt

    monkeypatch.setattr(d.adapter, "materialize", revoke_after_receipt)
    with pytest.raises(PermissionError):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert (await _execution_state(c)) == (("executing", 1, None), ("draft", 1), ("planned", 1), 1)

    monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", original_policy)
    monkeypatch.setattr(d.adapter, "materialize", original_adapter)
    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "unknown"
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_new_epoch_reuses_success_but_old_token_is_rejected(dispatch):
    d = dispatch
    c = d.c
    first = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    c.s.clock.advance(31)
    claimed = await c.worker.claim(c.job.job_id)
    token = lease_token(claimed)
    with pytest.raises((PermissionError, JobConflict)):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    replay = await d.service.materialize(token, c.args, c.plan, d.approval.approval_id)
    assert replay == first and replay.status == "succeeded"
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_success_replay_revalidates_ready_coupon_against_approved_plan(dispatch):
    d = dispatch
    c = d.c
    result = await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert result.status == "succeeded"
    async with c.s.db.business_sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE coupon_batches SET coupon_spec_hash = :changed "
                "WHERE tenant_id = :tenant AND coupon_batch_id = :coupon"
            ),
            {
                "changed": "sha256:" + "0" * 64,
                "tenant": c.job.tenant_id,
                "coupon": c.args.coupon_batch_id,
            },
        )

    with pytest.raises((PermissionError, ValueError)):
        await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_dispatch_holds_platform_and_business_sqlite_writes_only_during_mock(
    dispatch, monkeypatch
):
    d = dispatch
    c = d.c
    entered = asyncio.Event()
    release = asyncio.Event()
    original = d.adapter.materialize

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(d.adapter, "materialize", pause)

    async with DatabaseResources(c.s.config) as observer:
        dispatch_task = asyncio.create_task(
            d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id)
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        platform_started = asyncio.Event()
        business_started = asyncio.Event()

        async def write_platform():
            async with observer.platform_sessions.begin() as session:
                platform_started.set()
                await session.execute(
                    text(
                        "UPDATE jobs SET revision = revision WHERE tenant_id = :tenant "
                        "AND job_id = :job"
                    ),
                    {"tenant": c.job.tenant_id, "job": c.job.job_id},
                )

        async def write_business():
            async with observer.business_sessions.begin() as session:
                business_started.set()
                await session.execute(
                    text(
                        "UPDATE campaigns SET version = version WHERE tenant_id = :tenant "
                        "AND campaign_id = :campaign"
                    ),
                    {"tenant": c.job.tenant_id, "campaign": c.args.campaign_id},
                )

        platform_task = asyncio.create_task(write_platform())
        business_task = asyncio.create_task(write_business())
        await asyncio.wait_for(platform_started.wait(), timeout=2)
        await asyncio.wait_for(business_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert not platform_task.done() and not business_task.done()
        release.set()
        result = await asyncio.wait_for(dispatch_task, timeout=2)
        await asyncio.wait_for(asyncio.gather(platform_task, business_task), timeout=2)

    assert result.status == "succeeded"
    assert (await _execution_state(c))[0] == ("succeeded", 1, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_timeout", [True, False, 0, 5.1, "1"])
async def test_dispatch_timeout_configuration_is_strict(dispatch, invalid_timeout):
    c = dispatch.c
    with pytest.raises(ValueError):
        JobCouponDispatchService(
            c.worker, c.s.db, InMemoryCouponBatchAdapter(), timeout_seconds=invalid_timeout
        )


@pytest.mark.asyncio
async def test_dispatch_rejects_subclass_adapters(dispatch):
    class NetworkLikeAdapter(InMemoryCouponBatchAdapter):
        pass

    c = dispatch.c
    with pytest.raises(ValueError):
        JobCouponDispatchService(c.worker, c.s.db, NetworkLikeAdapter())
