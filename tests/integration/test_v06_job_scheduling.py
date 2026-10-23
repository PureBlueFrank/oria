"""Explicit scheduling on real temporary SQLite; no poller or business replay."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from tests.integration.test_v06_job_leases import _events, _save, _trusted_guard
from tests.integration.test_v06_job_leases import leases as leases

from oria.api.identity import AuthenticationError
from oria.api.jobs import JobAPI
from oria.core.job_leases import lease_token
from oria.core.jobs import Job, JobConflict, JobUnavailable

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


async def _retry(s, *, timeout=3600, delay=10, max_attempts=3):
    job = await s.add(
        max_attempts=max_attempts,
        timeout_at=s.clock.now() + timedelta(seconds=timeout),
    )
    worker = s.service()
    running = await worker.claim(job.job_id)
    retry = await worker.schedule_retry(lease_token(running), delay_seconds=delay)
    return worker, running, retry


@pytest.mark.asyncio
async def test_due_retry_releases_once_preserves_checkpoint_and_consumes_one_attempt(leases):
    s = leases
    job = await s.add(max_attempts=2)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await _save(s, token, "before-retry")
    published = await worker.publish_checkpoint(token, "before-retry")
    retry = await worker.schedule_retry(token, delay_seconds=10)
    assert retry.status == "retry_scheduled" and retry.attempt == 1
    assert retry.lease_owner is retry.lease_expires_at is retry.heartbeat_at is None
    assert retry.accepted_checkpoint_id == published.accepted_checkpoint_id
    with pytest.raises(JobConflict):
        await worker.claim(job.job_id)
    with pytest.raises(JobConflict):
        await worker.release_retry(job.job_id)
    s.clock.advance(10)
    queued = await s.service("scheduler-after-restart").release_retry(job.job_id)
    assert queued.status == "queued" and queued.attempt == 2 and queued.retry_at is None
    with pytest.raises(JobConflict):
        await worker.release_retry(job.job_id)
    running = await worker.claim(job.job_id)
    assert running.attempt == running.lease_epoch == 2
    assert running.accepted_checkpoint_id == "before-retry"
    with pytest.raises(JobConflict):
        await worker.schedule_retry(lease_token(running), delay_seconds=1)
    with pytest.raises(JobConflict):
        await worker.heartbeat(token)
    assert await _events(s, job.job_id) == [
        (0, "queued"),
        (1, "running"),
        (2, "running"),
        (3, "retry_scheduled"),
        (4, "queued"),
        (5, "running"),
    ]


@pytest.mark.asyncio
async def test_concurrent_retry_releases_use_one_budget_slot(leases):
    s = leases
    worker, _, retry = await _retry(s)
    s.clock.advance(10)
    results = await asyncio.gather(
        worker.release_retry(retry.job_id),
        s.service("other").release_retry(retry.job_id),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Job) for result in results) == 1
    assert sum(isinstance(result, JobConflict) for result in results) == 1
    current = await s.repo.get(retry.tenant_id, retry.job_id)
    assert current.attempt == 2 and current.revision == 3
    assert len(await _events(s, retry.job_id)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0, -1, True, 1.5, 86401])
async def test_invalid_retry_delay_does_not_change_running_job(leases, delay):
    s = leases
    job = await s.add()
    worker = s.service()
    running = await worker.claim(job.job_id)
    with pytest.raises(ValueError):
        await worker.schedule_retry(lease_token(running), delay_seconds=delay)
    assert await s.repo.get(job.tenant_id, job.job_id) == running


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "budget", "expired", "cancelled", "foreign"])
async def test_retry_requires_current_lease_authority_and_available_budget(leases, mode):
    s = leases
    job = await s.add(
        max_attempts=1 if mode == "budget" else 3,
        timeout_at=s.clock.now() + timedelta(seconds=60),
    )
    worker = s.service()
    running = await worker.claim(job.job_id)
    token = lease_token(running)
    if mode == "expired":
        s.clock.advance(30)
    elif mode == "cancelled":
        await JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now).cancel(
            job.job_id, s.owner, "cancel-before-retry"
        )
    before = await s.repo.get(job.tenant_id, job.job_id)
    target = s.service("other") if mode == "foreign" else worker
    with pytest.raises((JobConflict, PermissionError)):
        await target.schedule_retry(token, delay_seconds=60 if mode == "deadline" else 1)
    assert await s.repo.get(job.tenant_id, job.job_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting_approval", "waiting_event"])
async def test_release_retry_cannot_resume_a_waiting_graph(leases, status):
    s = leases
    job = await s.add(max_attempts=1)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await _save(s, token, "waiting")
    await worker.publish_checkpoint(token, "waiting")
    waiting = await worker.finish(token, status)
    with pytest.raises(JobConflict):
        await worker.release_retry(job.job_id)
    assert await s.repo.get(job.tenant_id, job.job_id) == waiting


@pytest.mark.asyncio
async def test_generic_replace_cannot_disguise_retry_as_free_wait_continuation(leases):
    s = leases
    _, _, retry = await _retry(s)
    s.clock.advance(10)
    queued = Job.model_validate(
        retry.model_dump()
        | {
            "status": "queued",
            "retry_at": None,
            "revision": retry.revision + 1,
            "updated_at": s.clock.now(),
        }
    )
    with pytest.raises(JobConflict, match="scheduler"):
        await s.repo.replace(retry, queued, _trusted_guard)
    assert await s.repo.get(retry.tenant_id, retry.job_id) == retry


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "retry_scheduled", "waiting_event"])
async def test_absolute_timeout_closes_nonterminal_jobs_without_moving_checkpoint(leases, status):
    s = leases
    job = await s.add(timeout_at=s.clock.now() + timedelta(seconds=20))
    worker = s.service()
    if status != "queued":
        token = lease_token(await worker.claim(job.job_id))
        await _save(s, token, "accepted-before-timeout")
        await worker.publish_checkpoint(token, "accepted-before-timeout")
        if status == "retry_scheduled":
            await worker.schedule_retry(token, delay_seconds=5)
        elif status == "waiting_event":
            await worker.finish(token, "waiting_event")
    before = await s.repo.get(job.tenant_id, job.job_id)
    with pytest.raises(JobConflict):
        await worker.expire(job.job_id)
    s.clock.advance(20)
    expired = await s.service("cleanup").expire(job.job_id)
    assert expired.status == "failed" and expired.lease_owner is expired.retry_at is None
    assert expired.attempt == before.attempt
    assert expired.accepted_checkpoint_id == before.accepted_checkpoint_id
    assert expired.revision == before.revision + 1
    assert await worker.expire(job.job_id) == expired
    with pytest.raises(JobConflict):
        await worker.claim(job.job_id)


@pytest.mark.asyncio
async def test_cancellation_is_finalized_only_after_lease_expiry_and_beats_timeout(leases):
    s = leases
    job = await s.add(timeout_at=s.clock.now() + timedelta(seconds=30))
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now).cancel(
        job.job_id, s.owner, "cancel"
    )
    with pytest.raises(JobConflict):
        await s.service("cleanup").expire(job.job_id)
    s.clock.advance(30)
    expired = await s.service("cleanup").expire(job.job_id)
    assert expired.status == "cancelled" and expired.cancel_requested
    with pytest.raises(JobConflict):
        await worker.finish(token, "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["retry", "release_retry", "expire"])
@pytest.mark.parametrize("failure", ["event", "identity", "policy"])
async def test_scheduling_rolls_back_state_and_event_on_failure(
    leases, monkeypatch, operation, failure
):
    s = leases
    worker, running, retry = await _retry(s)
    if operation == "retry":
        s.clock.advance(10)
        await worker.release_retry(retry.job_id)
        before = await worker.claim(retry.job_id)

        async def invoke():
            return await worker.schedule_retry(lease_token(before), delay_seconds=10)
    else:
        before = retry
        s.clock.advance(10 if operation == "release_retry" else 3600)

        async def invoke():
            return await getattr(worker, operation)(retry.job_id)

    events = await _events(s, running.job_id)
    append = s.repo._append_event

    async def disrupt(session, value):
        await append(session, value)
        if failure == "event":
            raise SQLAlchemyError("private-sql-and-parameters")
        if failure == "identity":
            s.path.write_text('{"bindings": []}')
        else:
            monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())

    monkeypatch.setattr(s.repo, "_append_event", disrupt)
    with pytest.raises((JobUnavailable, AuthenticationError, PermissionError)) as error:
        await invoke()
    assert "private-sql" not in str(error.value)
    assert await s.repo.get(retry.tenant_id, retry.job_id) == before
    assert await _events(s, retry.job_id) == events


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["retry", "release_retry"])
async def test_deadline_crossing_while_transaction_waits_rolls_back(leases, monkeypatch, operation):
    s = leases
    worker, running, retry = await _retry(s, timeout=20, delay=5)
    if operation == "retry":
        s.clock.advance(5)
        await worker.release_retry(retry.job_id)
        before = await worker.claim(retry.job_id)

        async def invoke():
            return await worker.schedule_retry(lease_token(before), delay_seconds=5)
    else:
        s.clock.advance(5)
        before = retry

        async def invoke():
            return await worker.release_retry(retry.job_id)

    events = await _events(s, running.job_id)
    append = s.repo._append_event

    async def cross_deadline(session, value):
        await append(session, value)
        s.clock.advance(15)

    monkeypatch.setattr(s.repo, "_append_event", cross_deadline)
    with pytest.raises(JobConflict):
        await invoke()
    assert await s.repo.get(retry.tenant_id, retry.job_id) == before
    assert await _events(s, retry.job_id) == events


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "policy", "clock"])
async def test_final_commit_guard_rechecks_after_last_async_guard(leases, monkeypatch, change):
    s = leases
    worker, _, retry = await _retry(s, timeout=20, delay=5)
    s.clock.advance(5)
    append = s.repo._append_event

    async def arm_commit_change(session, value):
        await append(session, value)

        def change_before_final_guard(_):
            if change == "identity":
                s.path.write_text('{"bindings": []}')
            elif change == "policy":
                monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())
            else:
                s.clock.advance(15)

        event.listen(session.sync_session, "before_commit", change_before_final_guard, insert=True)

    monkeypatch.setattr(s.repo, "_append_event", arm_commit_change)
    with pytest.raises((JobConflict, AuthenticationError, PermissionError)):
        await worker.release_retry(retry.job_id)
    assert await s.repo.get(retry.tenant_id, retry.job_id) == retry
    assert len(await _events(s, retry.job_id)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["release_retry", "expire"])
async def test_cancel_wins_against_a_stale_scheduler_snapshot(leases, monkeypatch, operation):
    s = leases
    worker, _, retry = await _retry(s, timeout=20, delay=5)
    s.clock.advance(5 if operation == "release_retry" else 20)
    replace = s.repo.replace_scheduled

    async def cancel_then_replace(*args, **kwargs):
        await JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now).cancel(
            retry.job_id, s.owner, "cancel-before-scheduler-cas"
        )
        return await replace(*args, **kwargs)

    monkeypatch.setattr(s.repo, "replace_scheduled", cancel_then_replace)
    with pytest.raises(JobConflict):
        await getattr(worker, operation)(retry.job_id)
    current = await s.repo.get(retry.tenant_id, retry.job_id)
    assert current.status == "cancelled" and current.attempt == 1
    assert await _events(s, retry.job_id) == [
        (0, "queued"),
        (1, "running"),
        (2, "retry_scheduled"),
        (3, "cancelled"),
    ]
