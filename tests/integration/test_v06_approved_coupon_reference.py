"""Approved local reference acceptance, real temporary records, no coupon effects."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.integration.approved_coupon_reference import (
    _ApprovedCouponReceiver,
    execute_approved_reference,
)
from tests.integration.test_v06_job_coupon_dispatch import _execution_state
from tests.integration.test_v06_job_coupon_dispatch import dispatch as dispatch
from tests.integration.test_v06_job_launch_approval import launch as launch
from tests.integration.test_v06_job_leases import leases as leases

from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


async def _call(d, path, *, lookup=False, approval_id=None, worker=None, token=None):
    c = d.c
    return await execute_approved_reference(
        worker or c.worker,
        c.s.db,
        path,
        token or c.token,
        c.args,
        c.plan,
        approval_id or d.approval.approval_id,
        lookup_only=lookup,
    )


def _count(path):
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT count(*) FROM history").fetchone()[0]


@pytest.mark.asyncio
async def test_approved_reference_durable_across_epoch_without_business_projection(
    dispatch, tmp_path
):
    d, path = dispatch, tmp_path / "approved.db"
    before = await _execution_state(d.c)
    assert (await _call(d, path, lookup=True)).state == "not_found"
    first = await _call(d, path)
    assert first.state == "succeeded"
    assert await _call(d, path) == first
    d.c.s.clock.advance(31)
    worker = d.c.s.service(instance="replacement")
    job = await worker.claim(d.c.job.job_id)
    assert await _call(d, path, lookup=True, worker=worker, token=lease_token(job)) == first
    with pytest.raises(JobConflict):
        await _call(d, path)
    assert _count(path) == 1
    assert await _execution_state(d.c) == before
    assert d.adapter.materialize_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "invalidated",
        "requester",
        "checkpoint",
        "hash",
        "policy",
        "expired",
        "coupon",
        "saga",
        "reservation",
    ],
)
async def test_invalid_approval_or_domain_facts_deny_even_history(
    dispatch, tmp_path, mutation, history
):
    d, path = dispatch, tmp_path / "approved.db"
    c = d.c
    if history:
        await _call(d, path)
    approval_id = None
    platform_changes = {
        "invalidated": ("status", "invalidated"),
        "requester": ("requester", "different-requester"),
        "checkpoint": ("checkpoint_id", "unrelated-checkpoint"),
        "hash": ("canonical_args_hash", "sha256:" + "b" * 64),
        "policy": ("policy_version", "changed-policy"),
        "expired": ("expires_at", (c.s.clock.now() + timedelta(seconds=1)).isoformat()),
    }
    if mutation == "missing":
        approval_id = "missing-approval"
    elif mutation in platform_changes:
        field, value = platform_changes[mutation]
        async with c.s.db.platform_sessions.begin() as session:
            await session.execute(
                text(
                    f"UPDATE approvals SET {field} = :value "
                    "WHERE tenant_id = :tenant AND approval_id = :approval"
                ),
                {"value": value, "tenant": c.job.tenant_id, "approval": d.approval.approval_id},
            )
        if mutation == "expired":
            c.s.clock.advance(2)
    else:
        statements = {
            "coupon": "UPDATE coupon_batches SET version = version + 1 "
            "WHERE tenant_id = :tenant AND coupon_batch_id = :resource",
            "saga": "UPDATE launch_saga_states SET version = version + 1 "
            "WHERE tenant_id = :tenant AND campaign_id = :resource",
            "reservation": "UPDATE tool_executions SET canonical_args_hash = :hash "
            "WHERE tenant_id = :tenant AND execution_id = :resource",
        }
        resources = {
            "coupon": c.args.coupon_batch_id,
            "saga": c.args.campaign_id,
            "reservation": d.reservation.execution_id,
        }
        async with c.s.db.business_sessions.begin() as session:
            await session.execute(
                text(statements[mutation]),
                {
                    "tenant": c.job.tenant_id,
                    "resource": resources[mutation],
                    "hash": "sha256:" + "c" * 64,
                },
            )
    before = await _execution_state(c)
    with pytest.raises(PermissionError):
        await _call(d, path, lookup=history, approval_id=approval_id)
    assert _count(path) == int(history)
    assert await _execution_state(c) == before
    assert d.adapter.materialize_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("mutation", ["policy_version", "expiry"])
async def test_approval_rechecked_at_reference_commit(
    dispatch, tmp_path, monkeypatch, history, mutation
):
    d, path = dispatch, tmp_path / "approved.db"
    c = d.c
    if mutation == "expiry":
        async with c.s.db.platform_sessions.begin() as session:
            await session.execute(
                text("UPDATE approvals SET expires_at = :expires WHERE approval_id = :approval"),
                {
                    "expires": (c.s.clock.now() + timedelta(seconds=1)).isoformat(),
                    "approval": d.approval.approval_id,
                },
            )
    if history:
        await _call(d, path)
    original = _ApprovedCouponReceiver._authorize
    evaluate = c.s.runtime.policy.evaluate_now
    calls = 0

    def invalidate(receiver, db, request):
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation == "expiry":
                c.s.clock.advance(2)
            else:

                def changed(request, ctx):
                    return evaluate(request, ctx).model_copy(update={"policy_version": "changed"})

                monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", changed)
        original(receiver, db, request)

    monkeypatch.setattr(_ApprovedCouponReceiver, "_authorize", invalidate)
    before = await _execution_state(c)
    with pytest.raises(PermissionError):
        await _call(d, path, lookup=history)
    assert calls == 2
    assert _count(path) == int(history)
    assert await _execution_state(c) == before


@pytest.mark.asyncio
async def test_approval_and_business_locks_cover_reference_commit(dispatch, tmp_path, monkeypatch):
    import subprocess
    import sys

    d, path = dispatch, tmp_path / "approved.db"
    c = d.c
    probe_script = """
import sqlite3,sys
from contextlib import closing
statements = {
    'approval': 'UPDATE approvals SET policy_version = policy_version WHERE approval_id = ?',
    'coupon': 'UPDATE coupon_batches SET version = version WHERE coupon_batch_id = ?',
}
with closing(sqlite3.connect(sys.argv[1], timeout=0)) as db:
    try:
        db.execute(statements[sys.argv[2]], (sys.argv[3],))
        db.commit()
        print('updated')
    except sqlite3.OperationalError as exc:
        assert 'locked' in str(exc)
        print('locked')
"""

    def probe(kind):
        database, identifier = (
            (c.s.config.data_paths.platform_db, d.approval.approval_id)
            if kind == "approval"
            else (c.s.config.data_paths.business_db, c.args.coupon_batch_id)
        )
        result = subprocess.run(
            [sys.executable, "-c", probe_script, str(database), kind, identifier],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()

    original = _ApprovedCouponReceiver._authorize
    observed = []

    def checked(receiver, db, request):
        original(receiver, db, request)
        observed.append((probe("approval"), probe("coupon")))

    monkeypatch.setattr(_ApprovedCouponReceiver, "_authorize", checked)
    before = await _execution_state(c)
    await _call(d, path)
    assert observed == [("locked", "locked"), ("locked", "locked")]
    assert (probe("approval"), probe("coupon")) == ("updated", "updated")
    assert await _execution_state(c) == before


@pytest.mark.asyncio
async def test_reference_cannot_mix_with_mock_dispatch_or_reuse_oria_database(dispatch, tmp_path):
    d, path = dispatch, tmp_path / "approved.db"
    for database in (d.c.s.config.data_paths.platform_db, d.c.s.config.data_paths.business_db):
        with pytest.raises(ValueError, match="separate file"):
            await _call(d, database)
    await d.service.materialize(d.c.token, d.c.args, d.c.plan, d.approval.approval_id)
    with pytest.raises(PermissionError, match="mix"):
        await _call(d, path)
    assert not path.exists()
