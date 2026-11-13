"""Source-level local receiver and atomic Oria projection; no real business target."""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.integration.test_v06_job_coupon_dispatch import _execution_state
from tests.integration.test_v06_job_coupon_dispatch import dispatch as dispatch
from tests.integration.test_v06_job_launch_approval import launch as launch
from tests.integration.test_v06_job_leases import leases as leases

from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict
from oria.orchestrator.job_coupon_dispatch import JobCouponDispatchService

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


def _service(d, path, worker=None):
    return JobCouponDispatchService.local_reference(worker or d.c.worker, d.c.s.db, path)


async def _call(d, service, *, reconcile=False, token=None):
    c = d.c
    return await (service.reconcile if reconcile else service.materialize)(
        token or c.token,
        c.args,
        c.plan,
        d.approval.approval_id,
    )


def _count(path):
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not names:
            return 0
        return db.execute("SELECT count(*) FROM local_coupon_history").fetchone()[0]


@pytest.mark.asyncio
async def test_source_reference_projects_once_and_rebuilds(dispatch, tmp_path):
    d, path = dispatch, tmp_path / "reference.db"
    service = _service(d, path)
    first = await _call(d, service)
    assert first.status == "succeeded" and first.receipt_id.startswith("localref_")
    assert (await _execution_state(d.c)) == (
        ("succeeded", 1, None),
        ("ready", 3),
        ("coupon_materialized", 2),
        2,
    )
    assert await _call(d, _service(d, path), reconcile=True) == first
    assert await _call(d, service) == first
    assert _count(path) == 1
    assert d.adapter.materialize_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "cancel", "projection"])
async def test_persistent_result_recovers_after_lost_response_or_projection(
    dispatch, tmp_path, monkeypatch, failure
):
    d, path = dispatch, tmp_path / "reference.db"
    service = _service(d, path)
    original = service._reference.observe
    committed = False

    def lose(*args, **kwargs):
        nonlocal committed
        result = original(*args, **kwargs)
        committed = True
        if failure == "response":
            raise RuntimeError("synthetic lost response")
        if failure == "cancel":
            raise asyncio.CancelledError
        return result

    engine = d.c.s.db.business_sessions.kw["bind"].sync_engine

    def fail_projection(session):
        if committed and failure == "projection" and session.bind is engine:
            raise RuntimeError("synthetic projection failure")

    event.listen(Session, "before_commit", fail_projection)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(service._reference, "observe", lose)
            if failure == "response":
                assert (await _call(d, service)).status == "unknown"
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
                    await _call(d, service)
    finally:
        event.remove(Session, "before_commit", fail_projection)
    assert _count(path) == 1
    assert (await _execution_state(d.c))[1:3] == (("draft", 1), ("planned", 1))
    d.c.s.clock.advance(31)
    worker = d.c.s.service(instance="reference-takeover")
    job = await worker.claim(d.c.job.job_id)
    result = await _call(d, _service(d, path, worker), reconcile=True, token=lease_token(job))
    assert result.status == "succeeded" and result.attempt_count == 1
    assert _count(path) == 1
    assert (await _execution_state(d.c))[1:3] == (("ready", 3), ("coupon_materialized", 2))


@pytest.mark.asyncio
async def test_unknown_without_receiver_history_never_redispatches(dispatch, tmp_path, monkeypatch):
    d, path = dispatch, tmp_path / "reference.db"
    service = _service(d, path)
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            raise OSError("synthetic unavailable receiver")

        patch.setattr(service._reference, "observe", unavailable)
        assert (await _call(d, service)).status == "unknown"
    assert not path.exists()
    for reconcile in (False, True, True):
        assert (await _call(d, _service(d, path), reconcile=reconcile)).status == "unknown"
    assert not path.exists()
    assert (await _execution_state(d.c))[1:3] == (("draft", 1), ("planned", 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "policy", "lease", "approval"])
async def test_reference_commit_authority_loss_rolls_back_effect(
    dispatch, tmp_path, monkeypatch, failure
):
    d, path = dispatch, tmp_path / "reference.db"
    service = _service(d, path)
    original = service._reference.observe
    evaluate = d.c.s.runtime.policy.evaluate_now

    def observe(request, args, *, admit, check, clock):
        count = 0

        def final_check():
            nonlocal count
            count += 1
            if count == 2:
                if failure == "identity":
                    d.c.s.path.write_text('{"bindings":[]}')
                elif failure == "lease":
                    d.c.s.clock.advance(31)
                else:

                    def changed(request, ctx):
                        return evaluate(request, ctx).model_copy(
                            update=(
                                {"allow": False}
                                if failure == "policy"
                                else {"policy_version": "changed"}
                            )
                        )

                    monkeypatch.setattr(d.c.s.runtime.policy, "evaluate_now", changed)
            check()

        return original(request, args, admit=admit, check=final_check, clock=clock)

    monkeypatch.setattr(service._reference, "observe", observe)
    with pytest.raises((PermissionError, JobConflict)):
        await _call(d, service)
    assert _count(path) == 0
    assert (await _execution_state(d.c))[0][0] == "executing"
    assert (await _execution_state(d.c))[1:3] == (("draft", 1), ("planned", 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["approval", "coupon", "reservation"])
async def test_reference_reuses_domain_checks_before_admission(dispatch, tmp_path, mutation):
    d, path = dispatch, tmp_path / "reference.db"
    c = d.c
    if mutation == "approval":
        async with c.s.db.platform_sessions.begin() as session:
            await session.execute(text("UPDATE approvals SET status = 'invalidated'"))
    else:
        async with c.s.db.business_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE coupon_batches SET version = version + 1"
                    if mutation == "coupon"
                    else "UPDATE tool_executions SET canonical_args_hash = :hash "
                    "WHERE execution_id = :execution"
                ),
                {"hash": "sha256:" + "b" * 64, "execution": d.reservation.execution_id},
            )
    before = await _execution_state(c)
    with pytest.raises(PermissionError):
        await _call(d, _service(d, path))
    assert not path.exists()
    assert await _execution_state(c) == before


@pytest.mark.asyncio
async def test_rejects_other_backend_history_and_oria_database_aliases(dispatch, tmp_path):
    d = dispatch
    for database in (d.c.s.config.data_paths.platform_db, d.c.s.config.data_paths.business_db):
        with pytest.raises(ValueError, match="separate"):
            _service(d, database)
    alias = tmp_path / "hardlink.db"
    alias.hardlink_to(d.c.s.config.data_paths.business_db)
    with pytest.raises(ValueError, match="separate"):
        _service(d, alias)
    await _call(d, d.service)
    path = tmp_path / "reference.db"
    with pytest.raises(PermissionError, match="another receiver"):
        await _call(d, _service(d, path), reconcile=True)
    assert not path.exists()


@pytest.mark.asyncio
async def test_foreign_sqlite_file_is_not_modified(dispatch, tmp_path):
    path = tmp_path / "foreign.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.commit()
    before = path.read_bytes()
    assert (await _call(dispatch, _service(dispatch, path))).status == "unknown"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_fresh_process_recovers_source_receiver_and_projects(dispatch, tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    d, path = dispatch, tmp_path / "reference.db"
    service = _service(d, path)
    original = service._reference.observe

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(service._reference, "observe", lose_response)
    assert (await _call(d, service)).status == "unknown"
    script = """
import asyncio,json,sys
from pathlib import Path
from datetime import datetime
from oria.api.identity import file_directory
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import Principal
from oria.core.job_leases import JobLeaseToken
from oria.domain.launch import MaterializeCouponBatchArgs
from oria.domain.ledger import LaunchPlan
from oria.orchestrator.job_coupon_dispatch import JobCouponDispatchService
from oria.orchestrator.job_leases import JobLeaseService
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository

async def main():
    data=json.load(sys.stdin)
    config=resolve_runtime_config(environ={}, data_dir=Path(data['root']))
    directory=file_directory(Path(data['directory']))
    bindings=directory().bindings
    worker=Principal.model_validate(data['worker'])
    clock=lambda: datetime.fromisoformat(data['now'])
    async with (
        DatabaseResources(config) as db,
        await build_runtime(
            config, resource_factories=(lambda: db,),
            trusted_actors=tuple(b.actor for b in bindings),
            trusted_executors=tuple(b.executor for b in bindings)+(worker,), clock=clock,
        ) as runtime,
    ):
        leases=JobLeaseService(runtime, JobRepository(db.platform_sessions), directory,
                               executor=worker, instance_id='worker-a', clock=clock)
        service=JobCouponDispatchService.local_reference(leases, db, Path(data['receiver']))
        result=await service.reconcile(JobLeaseToken.model_validate(data['token']),
            MaterializeCouponBatchArgs.model_validate(data['args']),
            LaunchPlan.model_validate(data['plan']), data['approval'])
        print(result.model_dump_json())
asyncio.run(main())
"""
    data = {
        "root": str(d.c.s.config.data_paths.root),
        "directory": str(d.c.s.path),
        "worker": d.c.s.worker.model_dump(mode="json"),
        "now": d.c.s.clock.now().isoformat(),
        "receiver": str(path),
        "token": d.c.token.model_dump(mode="json"),
        "args": d.c.args.model_dump(mode="json"),
        "plan": d.c.plan.model_dump(mode="json"),
        "approval": d.approval.approval_id,
    }
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "succeeded" and result["attempt_count"] == 1
    assert result["receipt_id"].startswith("localref_")
    assert _count(path) == 1
    assert (await _execution_state(d.c))[1:3] == (("ready", 3), ("coupon_materialized", 2))
