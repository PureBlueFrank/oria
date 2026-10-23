"""Automatic maintenance on real temporary SQLite, without Graph or network calls."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.integration.test_v06_job_leases import _events, _save
from tests.integration.test_v06_job_leases import leases as leases

from oria.api.jobs import JobAPI
from oria.core.job_leases import lease_token
from oria.core.jobs import JobUnavailable
from oria.orchestrator.job_scheduler import JobScheduler

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


@pytest.mark.asyncio
async def test_restart_releases_due_retry_once_and_preserves_accepted_checkpoint(leases):
    s = leases
    job = await s.add()
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await _save(s, token, "accepted-before-retry")
    await worker.publish_checkpoint(token, "accepted-before-retry")
    retry = await worker.schedule_retry(token, delay_seconds=5)
    assert (await JobScheduler(worker).run_once()).skipped == 1
    s.clock.advance(5)
    result = await JobScheduler(s.service("restarted-scheduler")).run_once()
    assert result.scanned == result.released == 1
    queued = await s.repo.get(job.tenant_id, job.job_id)
    assert queued.status == "queued" and queued.attempt == 2
    assert queued.accepted_checkpoint_id == retry.accepted_checkpoint_id
    assert queued.lease_epoch == retry.lease_epoch
    assert (await JobScheduler(worker).run_once()).skipped == 1
    assert len(await _events(s, job.job_id)) == 5


@pytest.mark.asyncio
async def test_scheduler_expires_deadline_and_cancelled_owner_but_keeps_valid_waits(leases):
    s = leases
    worker = s.service()
    due = await s.add(timeout_at=s.clock.now() + timedelta(seconds=10))
    cancelled = await s.add()
    await worker.claim(cancelled.job_id)
    await JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now).cancel(
        cancelled.job_id, s.owner, "scheduler-cancel"
    )
    waiting = await s.add()
    token = lease_token(await worker.claim(waiting.job_id))
    await _save(s, token, "waiting-for-event")
    await worker.publish_checkpoint(token, "waiting-for-event")
    wait = await worker.finish(token, "waiting_event")
    s.clock.advance(30)
    result = await JobScheduler(worker).run_once()
    assert result.scanned == 3 and result.expired == 2 and result.skipped == 1
    assert (await s.repo.get(due.tenant_id, due.job_id)).status == "failed"
    assert (await s.repo.get(cancelled.tenant_id, cancelled.job_id)).status == "cancelled"
    assert await s.repo.get(waiting.tenant_id, waiting.job_id) == wait


@pytest.mark.asyncio
async def test_pagination_passes_denied_rows_and_eventually_wraps(leases, monkeypatch):
    s = leases
    jobs = [
        await s.add(
            job_id=f"job_{number:032x}",
            actor=s.owner if number == 1 else s.admin,
            timeout_at=s.clock.now() + timedelta(seconds=1),
        )
        for number in range(1, 4)
    ]
    s.clock.advance(1)
    monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset({s.admin.actor}))
    scheduler = JobScheduler(s.service(), batch_size=1)
    assert (await scheduler.run_once()).denied == 1
    assert (await scheduler.run_once()).expired == 1
    assert (await scheduler.run_once()).expired == 1
    assert (await scheduler.run_once()).scanned == 0
    assert (await scheduler.run_once()).denied == 1
    assert await s.repo.get(jobs[0].tenant_id, jobs[0].job_id) == jobs[0]
    assert await _events(s, jobs[0].job_id) == [(0, "queued")]


@pytest.mark.asyncio
async def test_scheduler_is_tenant_scoped_even_without_database_rls(leases):
    s = leases
    await s.add()
    # SQLite has no RLS; even its scheduling reference query must scope by tenant.
    assert await s.repo.scheduling_candidates("foreign-tenant") == ()
    assert (await JobScheduler(s.service()).run_once()).scanned == 1


@pytest.mark.asyncio
async def test_two_schedulers_release_a_retry_without_duplicate_budget_or_events(leases):
    s = leases
    job = await s.add()
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await worker.schedule_retry(token, delay_seconds=1)
    s.clock.advance(1)
    results = await asyncio.gather(
        JobScheduler(worker).run_once(), JobScheduler(s.service("another")).run_once()
    )
    assert sum(r.released for r in results) == 1
    queued = await s.repo.get(job.tenant_id, job.job_id)
    assert queued.status == "queued" and queued.attempt == 2
    assert await _events(s, job.job_id) == [
        (0, "queued"),
        (1, "running"),
        (2, "retry_scheduled"),
        (3, "queued"),
    ]


@pytest.mark.asyncio
async def test_corrupt_row_is_counted_without_blocking_other_jobs(leases):
    s = leases
    bad = await s.add(job_id=f"job_{1:032x}")
    good = await s.add(job_id=f"job_{2:032x}", timeout_at=s.clock.now() + timedelta(seconds=1))
    async with s.db.platform_sessions.begin() as session:
        await session.execute(
            text("UPDATE jobs SET request_json = :broken WHERE job_id = :job"),
            {"broken": "not-json", "job": bad.job_id},
        )
    s.clock.advance(1)
    result = await JobScheduler(s.service()).run_once()
    assert result.scanned == 2 and result.unavailable == result.expired == 1
    assert (await s.repo.get(good.tenant_id, good.job_id)).status == "failed"
    assert "not-json" not in repr(result)


@pytest.mark.asyncio
async def test_polling_recovers_after_scan_outage_and_stops(leases, monkeypatch):
    s = leases
    job = await s.add(timeout_at=s.clock.now() + timedelta(seconds=1))
    s.clock.advance(1)
    scheduler = JobScheduler(s.service(), interval_seconds=0.01)
    scan = s.repo.scheduling_candidates
    calls = 0

    async def unavailable_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobUnavailable("private error must not be recorded")
        return await scan(*args, **kwargs)

    monkeypatch.setattr(s.repo, "scheduling_candidates", unavailable_once)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    try:
        async with asyncio.timeout(5):
            while (await s.repo.get(job.tenant_id, job.job_id)).status != "failed":
                await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        stop.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert calls >= 2
    assert len(await _events(s, job.job_id)) == 2


@pytest.mark.asyncio
async def test_pre_stopped_loop_never_scans_and_task_cancellation_propagates(leases, monkeypatch):
    scheduler = JobScheduler(leases.service(), interval_seconds=60)
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "run_once", block)
    stop = asyncio.Event()
    stop.set()
    await scheduler.run(stop)
    assert not entered.is_set()
    stop.clear()
    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("size", [0, 101, True, 1.5])
def test_invalid_batch_size_is_rejected(size):
    with pytest.raises(ValueError):
        JobScheduler(None, batch_size=size)


@pytest.mark.parametrize("interval", [0, -1, 61, True, float("nan"), float("inf")])
def test_invalid_poll_interval_is_rejected(interval):
    with pytest.raises(ValueError):
        JobScheduler(None, interval_seconds=interval)
