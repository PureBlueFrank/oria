"""Actual temporary Job rows and identity/Policy, synthetic receiver effects only."""

from contextlib import asynccontextmanager, closing
from decimal import Decimal

import pytest
from sqlalchemy import text
from tests.contract.test_v06_dispatch_contract import _Payload
from tests.integration.job_authority_reference import _JobBoundReceiver, execute_reference
from tests.integration.test_v06_job_leases import leases as leases

from oria.api.identity import AuthenticationError
from oria.core.approvals import canonical_args_hash
from oria.core.dispatch import DispatchOperation, DispatchRejected, DispatchRequest
from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


def _request(job):
    coupon = f"coupon_{job.job_id.removeprefix('job_')}"
    payload = _Payload(coupon_id=coupon, amount=Decimal("10.00"))
    return DispatchRequest(
        operation=DispatchOperation(
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            adapter_id="fixture-coupon",
            tool_name="materialize_coupon_batch",
            operation_key=f"{coupon}:reference:v1",
            resource_ref=f"coupon_batch:{coupon}",
            tool_schema_version=1,
            canonical_args_hash=canonical_args_hash(
                tool_name="materialize_coupon_batch",
                tool_schema_version=1,
                schema=_Payload,
                args=payload.model_dump(),
            ),
        ),
        fence=lease_token(job),
    ), payload


async def _claimed(s):
    job = await s.add(scenario="campaign", actor=s.admin)
    service = s.service()
    claimed = await service.claim(job.job_id)
    return service, claimed


@pytest.mark.asyncio
async def test_durable_history_survives_rebuilt_service_and_epoch(leases, tmp_path):
    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    path = tmp_path / "reference.db"
    result = await execute_reference(service, path, request, payload)
    assert result.state == "succeeded"
    assert await s.repo.get(job.tenant_id, job.job_id) == job
    assert await execute_reference(s.service(), path, request) == result
    s.clock.advance(31)
    replacement = s.service(instance="worker-b")
    newer = await replacement.claim(job.job_id)
    new_request, _ = _request(newer)
    assert await execute_reference(replacement, path, new_request) == result
    with pytest.raises(JobConflict):
        await execute_reference(service, path, request, payload)
    import sqlite3

    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1
        # No copied synthetic authority is used by the integrated bridge.
        assert db.execute("SELECT count(*) FROM authority").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "policy", "expiry"])
@pytest.mark.parametrize("history", [False, True])
async def test_current_authority_loss_before_receiver_commit_rolls_back(
    leases, tmp_path, monkeypatch, change, history
):
    import sqlite3

    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    path = tmp_path / "reference.db"
    if history:
        await execute_reference(service, path, request, payload)
    original = _JobBoundReceiver._authorize
    calls = 0

    def revoke_at_commit(receiver, db, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "identity":
                s.path.write_text('{"bindings": []}')
            elif change == "policy":
                monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())
            else:
                s.clock.advance(31)
        original(receiver, db, value)

    monkeypatch.setattr(_JobBoundReceiver, "_authorize", revoke_at_commit)
    with pytest.raises((PermissionError, AuthenticationError, DispatchRejected)):
        await execute_reference(service, path, request, None if history else payload)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == int(history)
    assert await s.repo.get(job.tenant_id, job.job_id) == job


@pytest.mark.asyncio
async def test_snapshot_changed_before_lock_is_not_authority(leases, tmp_path, monkeypatch):
    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    original = s.repo._hold_execution_lease

    @asynccontextmanager
    async def change_before_lock(expected, token, clock):
        async with s.db.platform_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE jobs SET cancel_requested = TRUE, revision = revision + 1 "
                    "WHERE job_id = :job AND tenant_id = :tenant"
                ),
                {"job": job.job_id, "tenant": job.tenant_id},
            )
        async with original(expected, token, clock) as session:
            yield session

    monkeypatch.setattr(s.repo, "_hold_execution_lease", change_before_lock)
    path = tmp_path / "reference.db"
    with pytest.raises(JobConflict):
        await execute_reference(service, path, request, payload)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["worker", "resource", "tenant", "scope"])
async def test_wrong_scope_rejected_without_reference_database(leases, tmp_path, change):
    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    if change == "worker":
        service = s.service(instance="other-worker")
    elif change == "resource":
        request = request.model_copy(
            update={
                "operation": request.operation.model_copy(
                    update={"resource_ref": "coupon_batch:other"}
                )
            }
        )
    elif change == "tenant":
        request = request.model_copy(
            update={
                "operation": request.operation.model_copy(update={"tenant_id": "other-tenant"}),
                "fence": request.fence.model_copy(update={"tenant_id": "other-tenant"}),
            }
        )
    else:
        job = await s.add()
        job = await service.claim(job.job_id)
        request, payload = _request(job)
    path = tmp_path / "reference.db"
    with pytest.raises(PermissionError):
        await execute_reference(service, path, request, payload)
    assert not path.exists()


@pytest.mark.asyncio
async def test_receiver_scope_cannot_outlive_platform_lock(leases, tmp_path, monkeypatch):
    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    captured = []
    original = _JobBoundReceiver.execute

    def capture(receiver, *args):
        captured.append(receiver)
        return original(receiver, *args)

    monkeypatch.setattr(_JobBoundReceiver, "execute", capture)
    await execute_reference(service, tmp_path / "reference.db", request, payload)
    with pytest.raises(PermissionError, match="scope ended"):
        captured[0].execute(request)
    with pytest.raises(PermissionError, match="owned by JobRepository"):
        captured[0].replace_authority(job)


@pytest.mark.asyncio
async def test_actual_platform_writer_blocked_until_receiver_commit(leases, tmp_path, monkeypatch):
    import subprocess
    import sys

    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    original = _JobBoundReceiver._authorize
    probe_script = """
import sqlite3,sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1], timeout=0)) as db:
    try:
        db.execute('UPDATE jobs SET lease_epoch = lease_epoch + 1 WHERE job_id = ?', (sys.argv[2],))
        db.commit()
        print('updated')
    except sqlite3.OperationalError as exc:
        assert 'locked' in str(exc)
        print('locked')
"""

    def probe():
        result = subprocess.run(
            [sys.executable, "-c", probe_script, str(s.config.data_paths.platform_db), job.job_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    checks = []

    def check_with_competing_writer(receiver, db, value):
        original(receiver, db, value)
        checks.append(probe())
        assert checks[-1] == "locked"

    monkeypatch.setattr(_JobBoundReceiver, "_authorize", check_with_competing_writer)
    await execute_reference(service, tmp_path / "reference.db", request, payload)
    assert checks == ["locked", "locked"]
    assert probe() == "updated"
    with pytest.raises(JobConflict):
        await execute_reference(service, tmp_path / "reference.db", request)


@pytest.mark.asyncio
async def test_response_loss_after_receiver_commit_keeps_history(leases, tmp_path, monkeypatch):
    import sqlite3

    s = leases
    service, job = await _claimed(s)
    request, payload = _request(job)
    path = tmp_path / "reference.db"
    original = s.repo._hold_execution_lease

    @asynccontextmanager
    async def lose_after_release(expected, token, clock):
        async with original(expected, token, clock) as session:
            yield session
        raise RuntimeError("synthetic response loss")

    with monkeypatch.context() as patch:
        patch.setattr(s.repo, "_hold_execution_lease", lose_after_release)
        with pytest.raises(RuntimeError, match="response loss"):
            await execute_reference(service, path, request, payload)
    result = await execute_reference(s.service(), path, request)
    assert result.state == "succeeded"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1
