"""Durable local outcome projection from retained Mock history, never a network target."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.integration.test_v06_job_coupon_dispatch import (
    _execution_state,
)
from tests.integration.test_v06_job_coupon_dispatch import (
    dispatch as dispatch,
)
from tests.integration.test_v06_job_launch_approval import launch as launch
from tests.integration.test_v06_job_leases import leases as leases

from oria.adapters.launch import InMemoryCouponBatchAdapter
from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict
from oria.orchestrator.job_coupon_dispatch import JobCouponDispatchService
from oria.orchestrator.job_leases import JobLeaseService
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


async def _lose_response(d, monkeypatch, *, orphan=False):
    original = d.adapter.materialize

    async def lose(*args, **kwargs):
        await original(*args, **kwargs)
        d.c.s.clock.advance(1)
        if orphan:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic lost response with secret text")

    with monkeypatch.context() as patch:
        patch.setattr(d.adapter, "materialize", lose)
        if orphan:
            with pytest.raises(asyncio.CancelledError):
                await d.service.materialize(d.c.token, d.c.args, d.c.plan, d.approval.approval_id)
        else:
            result = await d.service.materialize(
                d.c.token, d.c.args, d.c.plan, d.approval.approval_id
            )
            assert result.status == "unknown"


async def _reconcile(d, *, token=None, service=None):
    return await (service or d.service).reconcile(
        token or d.c.token, d.c.args, d.c.plan, d.approval.approval_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("status", ["accepted", "rejected"])
async def test_lost_response_reconciles_once_across_service_restart(
    dispatch, monkeypatch, orphan, status
):
    d = dispatch
    c = d.c
    d.adapter.materialize_status = status
    await _lose_response(d, monkeypatch, orphan=orphan)
    c.s.clock.advance(1)  # The receipt predates the later reconciliation transaction.
    async with DatabaseResources(c.s.config) as db:
        worker = JobLeaseService(
            c.s.runtime,
            JobRepository(db.platform_sessions),
            c.s.source,
            executor=c.s.worker,
            instance_id="worker-a",
            clock=c.s.clock.now,
        )
        service = JobCouponDispatchService(worker, db, d.adapter)
        first = await _reconcile(d, service=service)
    expected = "succeeded" if status == "accepted" else "failed"
    assert first.status == expected and first.compensation_status is None
    assert first.receipt_id and first.receipt_summary_hash and first.attempt_count == 1
    snapshot = await _execution_state(c)
    assert snapshot[0] == (expected, 1, None)
    assert snapshot[1] == (("ready", 3) if status == "accepted" else ("draft", 1))
    assert snapshot[2] == (("coupon_materialized", 2) if status == "accepted" else ("planned", 1))
    assert snapshot[3] == (2 if orphan else 3)
    assert await _reconcile(d) == first
    assert await d.service.materialize(c.token, c.args, c.plan, d.approval.approval_id) == first
    assert await _execution_state(c) == snapshot
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]
    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == c.claimed
    async with c.s.db.business_sessions() as session:
        for table in ("audit_events", "outbox"):
            # Each dispatch domain event has exactly one matching audit and outbox.
            column = "action" if table == "audit_events" else "topic"
            count = (
                await session.execute(
                    text(
                        f"SELECT count(*) FROM {table} "
                        f"WHERE {column} LIKE 'coupon_batch.job_dispatch.%'"
                    )
                )
            ).scalar_one()
            assert count == snapshot[3]


@pytest.mark.asyncio
async def test_reconcile_never_admits_reserved_operation(dispatch):
    d = dispatch
    with pytest.raises(ValueError, match="prior dispatch"):
        await _reconcile(d)
    assert (await _execution_state(d.c))[0] == ("reserved", 0, None)
    assert d.adapter.materialize_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unknown", "exception", "timeout"])
async def test_unresolved_lookup_keeps_history_and_never_retries(dispatch, monkeypatch, failure):
    d = dispatch
    if failure == "unknown":
        d.adapter.materialize_status = "unknown"
    await _lose_response(d, monkeypatch)
    before = await _execution_state(d.c)
    if failure == "missing":
        # A restarted in-memory receiver has no history, even if the effect occurred.
        d.service = JobCouponDispatchService(d.c.worker, d.c.s.db, InMemoryCouponBatchAdapter())
    elif failure in {"exception", "timeout"}:

        async def unavailable(*args, **kwargs):
            if failure == "timeout":
                await asyncio.sleep(0.05)
            raise RuntimeError("secret lookup response")

        monkeypatch.setattr(d.adapter, "lookup_materialization", unavailable)
        d.service._timeout = 0.001
    first = await _reconcile(d)
    second = await _reconcile(d)
    assert first == second and first.status == "unknown"
    assert await _execution_state(d.c) == before
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_orphan_without_receiver_history_becomes_unknown_once(dispatch, monkeypatch):
    d = dispatch
    await _lose_response(d, monkeypatch, orphan=True)
    service = JobCouponDispatchService(d.c.worker, d.c.s.db, InMemoryCouponBatchAdapter())
    result = await _reconcile(d, service=service)
    assert result.status == "unknown"
    assert (await _execution_state(d.c))[3] == 2
    assert await _reconcile(d, service=service) == result
    assert (await _execution_state(d.c))[3] == 2


@pytest.mark.asyncio
async def test_new_epoch_can_reconcile_but_old_epoch_cannot(dispatch, monkeypatch):
    d = dispatch
    await _lose_response(d, monkeypatch)
    d.c.s.clock.advance(31)
    token = lease_token(await d.c.worker.claim(d.c.job.job_id))
    with pytest.raises((PermissionError, JobConflict)):
        await _reconcile(d)
    result = await _reconcile(d, token=token)
    assert result.status == "succeeded" and result.attempt_count == 1
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["adapter", "resource", "future", "old", "invalid"])
async def test_bad_lookup_receipt_cannot_resolve_unknown(dispatch, monkeypatch, mutation):
    d = dispatch
    await _lose_response(d, monkeypatch)
    original = await d.adapter.lookup_materialization(
        d.c.args, idempotency_key=d.reservation.idempotency_key
    )
    changes = {
        "adapter": {"adapter_id": "other"},
        "resource": {"resource_ref": "coupon_batch:other"},
        "future": {"received_at": d.c.s.clock.now() + timedelta(seconds=1)},
        "old": {"received_at": d.reservation.created_at - timedelta(seconds=1)},
        "invalid": {"status": "invalid"},
    }

    async def malformed(*args, **kwargs):
        return original.model_copy(update=changes[mutation])

    monkeypatch.setattr(d.adapter, "lookup_materialization", malformed)
    before = await _execution_state(d.c)
    assert (await _reconcile(d)).status == "unknown"
    assert await _execution_state(d.c) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["policy", "identity", "lease", "approval"])
@pytest.mark.parametrize("boundary", ["lookup", "commit"])
async def test_reconciliation_rechecks_authority_and_rolls_back(
    dispatch, monkeypatch, failure, boundary
):
    d = dispatch
    c = d.c
    await _lose_response(d, monkeypatch)
    before = await _execution_state(c)
    original_lookup = d.adapter.lookup_materialization
    original_policy = c.s.runtime.policy.evaluate_now

    def invalidate():
        if failure == "policy":

            def deny(request, ctx):
                return original_policy(request, ctx).model_copy(update={"allow": False})

            monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", deny)
        elif failure == "identity":
            c.s.path.write_text('{"bindings":[]}')
        elif failure == "lease":
            c.s.clock.advance(31)
        else:

            def changed_version(request, ctx):
                return original_policy(request, ctx).model_copy(
                    update={"policy_version": "changed-approval-policy"}
                )

            monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", changed_version)

    async def invalidate_after_lookup(*args, **kwargs):
        receipt = await original_lookup(*args, **kwargs)
        invalidate()
        return receipt

    business_engine = c.s.db.business_sessions.kw["bind"].sync_engine

    def invalidate_before_commit(session):
        if session.bind is business_engine:
            invalidate()

    if boundary == "lookup":
        monkeypatch.setattr(d.adapter, "lookup_materialization", invalidate_after_lookup)
    else:
        event.listen(Session, "before_commit", invalidate_before_commit)
    try:
        with pytest.raises((PermissionError, JobConflict)):
            await _reconcile(d)
    finally:
        if boundary == "commit":
            event.remove(Session, "before_commit", invalidate_before_commit)
    assert await _execution_state(c) == before
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_reconciliation_event_failure_rolls_back_all_projections(dispatch, monkeypatch):
    d = dispatch
    await _lose_response(d, monkeypatch)
    before = await _execution_state(d.c)

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic outbox failure")

    with monkeypatch.context() as patch:
        patch.setattr(d.service._writer._outbox, "_append", fail)
        with pytest.raises(RuntimeError, match="synthetic outbox failure"):
            await _reconcile(d)
    assert await _execution_state(d.c) == before
    assert (await _reconcile(d)).status == "succeeded"
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
async def test_cancelled_lookup_does_not_change_unknown(dispatch, monkeypatch):
    d = dispatch
    await _lose_response(d, monkeypatch)
    before = await _execution_state(d.c)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(d.adapter, "lookup_materialization", cancel)
    with pytest.raises(asyncio.CancelledError):
        await _reconcile(d)
    assert await _execution_state(d.c) == before


@pytest.mark.asyncio
async def test_mock_history_binds_key_to_exact_payload(dispatch):
    d = dispatch
    key = d.reservation.idempotency_key
    original = await d.adapter.materialize(d.c.args, idempotency_key=key)
    d.adapter.materialize_status = "rejected"
    assert await d.adapter.materialize(d.c.args, idempotency_key=key) == original
    assert await d.adapter.lookup_materialization(d.c.args, idempotency_key="absent") is None
    altered = d.c.args.model_copy(update={"coupon_batch_id": "coupon_other"})
    with pytest.raises(ValueError, match="conflicts"):
        await d.adapter.lookup_materialization(altered, idempotency_key=key)
    with pytest.raises(ValueError, match="conflicts"):
        await d.adapter.materialize(altered, idempotency_key=key)
    assert d.adapter.materialize_calls == [key]


@pytest.mark.asyncio
async def test_independent_connections_reconcile_one_outcome(dispatch, monkeypatch):
    d = dispatch
    c = d.c
    await _lose_response(d, monkeypatch)
    async with DatabaseResources(c.s.config) as db:
        worker = JobLeaseService(
            c.s.runtime,
            JobRepository(db.platform_sessions),
            c.s.source,
            executor=c.s.worker,
            instance_id="worker-a",
            clock=c.s.clock.now,
        )
        other = JobCouponDispatchService(worker, db, d.adapter)
        first, second = await asyncio.gather(_reconcile(d), _reconcile(d, service=other))
    assert first == second and first.status == "succeeded"
    assert (await _execution_state(c))[3] == 3
    assert d.adapter.materialize_calls == [d.reservation.idempotency_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["tenant", "args", "approval", "cancel"])
async def test_invalid_reconciliation_is_denied_before_lookup(dispatch, monkeypatch, invalid):
    d = dispatch
    c = d.c
    await _lose_response(d, monkeypatch)
    before = await _execution_state(c)
    calls = []

    async def spy(*args, **kwargs):
        calls.append(True)
        raise AssertionError("invalid request reached lookup")

    monkeypatch.setattr(d.adapter, "lookup_materialization", spy)
    token, args, approval_id = c.token, c.args, d.approval.approval_id
    if invalid == "tenant":
        token = token.model_copy(update={"tenant_id": "other-tenant"})
    elif invalid == "args":
        args = args.model_copy(update={"coupon_spec_hash": "sha256:" + "0" * 64})
    elif invalid == "approval":
        approval_id = "missing-approval"
    else:
        async with c.s.db.platform_sessions.begin() as session:
            await session.execute(
                text("UPDATE jobs SET cancel_requested = 1 WHERE job_id = :job"),
                {"job": c.job.job_id},
            )
    with pytest.raises((PermissionError, JobConflict, ValueError, LookupError)):
        await d.service.reconcile(token, args, c.plan, approval_id)
    assert calls == []
    assert await _execution_state(c) == before
