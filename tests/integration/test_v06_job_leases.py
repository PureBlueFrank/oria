"""Real SQLite/Policy/saver lease races; no Graph runner or external side effects."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text
from tests.security.test_v06_api_identity import binding

from oria.api.identity import AuthenticationError, IdentityDirectory, file_directory
from oria.config import resolve_runtime_config
from oria.core.job_leases import lease_token
from oria.core.jobs import Job, JobConflict, JobSession, JobSubmission, submission_hash
from oria.core.runtime import build_runtime
from oria.orchestrator.checkpoint import open_tenant_sqlite_saver
from oria.orchestrator.job_leases import JobLeaseService
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


async def _trusted_guard():
    pass


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 22, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def leases(tmp_path):
    owner = binding()
    admin = binding(subject="admin").model_copy(
        update={
            "actor": owner.actor.model_copy(
                update={"subject_id": "campaign-admin", "roles": ("campaign_admin",)}
            )
        }
    )
    worker = owner.executor.model_copy(
        update={"subject_id": "oria-worker", "roles": ("job_worker",)}
    )
    path = tmp_path / "identities.json"
    directory = IdentityDirectory(bindings=(owner, admin))
    path.write_text(directory.model_dump_json())
    source = file_directory(path)
    clock = Clock()
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    from oria.migrations.runner import upgrade_databases

    upgrade_databases(config)
    async with (
        DatabaseResources(config) as db,
        open_tenant_sqlite_saver(config.data_paths.platform_db) as saver,
        await build_runtime(
            config,
            resource_factories=(lambda: db,),
            trusted_actors=(owner.actor, admin.actor),
            trusted_executors=(owner.executor, worker),
            clock=clock.now,
        ) as runtime,
    ):
        repo = JobRepository(db.platform_sessions)

        def service(instance="worker-a", **kwargs):
            return JobLeaseService(
                runtime,
                repo,
                source,
                executor=kwargs.pop("executor", worker),
                instance_id=instance,
                clock=clock.now,
                checkpointer=saver,
                **kwargs,
            )

        async def add(*, scenario="attribution", actor=owner, **changes):
            session_id = "session_" + uuid.uuid4().hex
            await repo.create_session(
                JobSession(
                    tenant_id=actor.actor.tenant_id,
                    session_id=session_id,
                    requester_subject_id=actor.actor.subject_id,
                    requester_issuer=actor.issuer,
                    requester_external_subject=actor.subject,
                    created_at=clock.now(),
                ),
                _trusted_guard,
            )
            spec = (
                {
                    "scenario": "attribution",
                    "metric": "redemptions",
                    "period": {"start_date": "2026-07-01", "end_date": "2026-07-07"},
                }
                if scenario == "attribution"
                else {
                    "scenario": "campaign",
                    "categories": ["餐饮"],
                    "cities": ["上海"],
                    "enrollment_mode": "auto",
                }
            )
            submission = JobSubmission.model_validate({"session_id": session_id, "spec": spec})
            values = {
                "tenant_id": actor.actor.tenant_id,
                "job_id": "job_" + uuid.uuid4().hex,
                "session_id": session_id,
                "thread_id": "thread_" + uuid.uuid4().hex,
                "run_id": "run_" + uuid.uuid4().hex,
                "requester_subject_id": actor.actor.subject_id,
                "requester_issuer": actor.issuer,
                "requester_external_subject": actor.subject,
                "submission": submission,
                "request_hash": submission_hash(submission),
                "idempotency_key": uuid.uuid4().hex * 2,
                "created_at": clock.now(),
                "updated_at": clock.now(),
                "timeout_at": clock.now() + timedelta(days=1),
            } | changes
            return await repo.submit(Job.model_validate(values), _trusted_guard)

        yield SimpleNamespace(
            repo=repo,
            db=db,
            clock=clock,
            source=source,
            path=path,
            owner=owner,
            admin=admin,
            worker=worker,
            runtime=runtime,
            config=config,
            service=service,
            add=add,
            saver=saver,
            directory=directory,
        )


async def _events(stack, job_id):
    async with stack.db.platform_sessions() as session:
        return (
            await session.execute(
                text(
                    "SELECT sequence, status FROM job_events WHERE job_id = :job ORDER BY sequence"
                ),
                {"job": job_id},
            )
        ).all()


async def _save(stack, token, checkpoint_id, *, parent=None, namespace=None):
    job = await stack.repo.get(token.tenant_id, token.job_id)
    config = parent or {
        "configurable": {
            "thread_id": job.thread_id,
            "oria_tenant_id": token.tenant_id,
            "checkpoint_ns": token.checkpoint_ns if namespace is None else namespace,
        }
    }
    value = empty_checkpoint()
    value["id"] = checkpoint_id
    return await stack.saver.aput(config, value, {"source": "loop", "step": 0, "parents": {}}, {})


@pytest.mark.asyncio
async def test_competing_claims_have_one_winner_and_heartbeat_keeps_attempt(leases):
    s = leases
    job = await s.add()
    services = [s.service("one"), s.service("two")]
    results = await asyncio.gather(
        *(service.claim(job.job_id) for service in services), return_exceptions=True
    )
    winners = [i for i, result in enumerate(results) if isinstance(result, Job)]
    assert len(winners) == 1 and sum(isinstance(result, JobConflict) for result in results) == 1
    winner = results[winners[0]]
    token = lease_token(winner)
    assert winner.attempt == winner.lease_epoch == 1
    s.clock.advance(5)
    renewed = await services[winners[0]].heartbeat(token)
    assert renewed.attempt == renewed.lease_epoch == 1 and renewed.revision == 2
    assert renewed.lease_expires_at == s.clock.now() + timedelta(seconds=30)
    done = await services[winners[0]].finish(token, "succeeded")
    assert done.status == "succeeded" and done.lease_owner is None
    assert await _events(s, job.job_id) == [
        (0, "queued"),
        (1, "running"),
        (2, "running"),
        (3, "succeeded"),
    ]


@pytest.mark.asyncio
async def test_expired_takeover_rejects_late_old_worker_writes(leases):
    s = leases
    job = await s.add()
    first, second = s.service("one"), s.service("two")
    token = lease_token(await first.claim(job.job_id))
    with pytest.raises(JobConflict):
        await second.claim(job.job_id)
    s.clock.advance(30)
    takeover = await second.claim(job.job_id)
    assert takeover.attempt == takeover.lease_epoch == 2
    for operation in (
        first.heartbeat(token),
        first.finish(token, "succeeded"),
        first.publish_checkpoint(token, "late"),
    ):
        with pytest.raises(JobConflict):
            await operation
    assert (await s.repo.get(job.tenant_id, job.job_id)) == takeover
    assert await _events(s, job.job_id) == [(0, "queued"), (1, "running"), (2, "running")]


@pytest.mark.asyncio
async def test_cooperative_cancel_only_accepts_cancelled_or_failed_completion(leases):
    from oria.api.jobs import JobAPI

    s = leases
    job = await s.add()
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    await JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now).cancel(
        job.job_id, s.owner, "cancel"
    )
    assert (await worker.heartbeat(token)).status == "cancellation_requested"
    with pytest.raises(JobConflict):
        await worker.finish(token, "succeeded")
    with pytest.raises(JobConflict):
        await worker.publish_checkpoint(token, "ignored")
    done = await worker.finish(token, "cancelled")
    assert done.cancel_requested and done.lease_owner is None
    with pytest.raises(JobConflict):
        await worker.claim(job.job_id)


@pytest.mark.asyncio
async def test_requester_revocation_during_claim_rolls_back_job_and_event(leases, monkeypatch):
    s = leases
    job = await s.add()
    append = s.repo._append_event

    async def revoke(session, value):
        await append(session, value)
        s.path.write_text('{"bindings": []}')

    monkeypatch.setattr(s.repo, "_append_event", revoke)
    with pytest.raises(AuthenticationError):
        await s.service().claim(job.job_id)
    assert await s.repo.get(job.tenant_id, job.job_id) == job
    assert await _events(s, job.job_id) == [(0, "queued")]


@pytest.mark.asyncio
async def test_policy_revoke_during_heartbeat_rolls_back_before_denial_audit(leases, monkeypatch):
    s = leases
    job = await s.add()
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    append = s.repo._append_event

    async def revoke(session, value):
        await append(session, value)
        monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())

    monkeypatch.setattr(s.repo, "_append_event", revoke)
    with pytest.raises(PermissionError):
        await worker.heartbeat(lease_token(claimed))
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed
    async with s.db.platform_sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'job:execute' "
                    "AND decision = 'deny'"
                )
            )
        ).scalar_one() == 1
    assert len(await _events(s, job.job_id)) == 2


@pytest.mark.asyncio
async def test_lease_expiry_while_suspended_rolls_back_heartbeat(leases, monkeypatch):
    s = leases
    job = await s.add()
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    s.clock.advance(10)
    append = s.repo._append_event

    async def suspend(session, value):
        await append(session, value)
        s.clock.advance(21)

    monkeypatch.setattr(s.repo, "_append_event", suspend)
    with pytest.raises(JobConflict):
        await worker.heartbeat(lease_token(claimed))
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed
    assert len(await _events(s, job.job_id)) == 2


@pytest.mark.asyncio
async def test_checkpoint_publication_requires_actual_epoch_and_advancing_lineage(leases):
    s = leases
    job = await s.add()
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    with pytest.raises(JobConflict):
        await worker.publish_checkpoint(token, "missing")
    await _save(s, token, "other-namespace", namespace="outside-epoch")
    with pytest.raises(JobConflict):
        await worker.publish_checkpoint(token, "other-namespace")
    root = await _save(s, token, "first")
    first = await worker.publish_checkpoint(token, "first")
    assert await worker.publish_checkpoint(token, "first") == first
    middle = await _save(s, token, "middle", parent=root)
    await _save(s, token, "latest", parent=middle)
    await worker.publish_checkpoint(token, "latest")
    await _save(s, token, "sibling", parent=root)
    for checkpoint_id in ("first", "middle", "sibling"):
        with pytest.raises(JobConflict):
            await worker.publish_checkpoint(token, checkpoint_id)
    waiting = await worker.finish(token, "waiting_event")
    assert waiting.accepted_checkpoint_id == "latest" and waiting.lease_owner is None
    assert len(await _events(s, job.job_id)) == 5


@pytest.mark.asyncio
async def test_late_checkpoint_stays_orphan_after_takeover(leases):
    s = leases
    job = await s.add()
    first, second = s.service("one"), s.service("two")
    old = lease_token(await first.claim(job.job_id))
    await _save(s, old, "accepted-old")
    await first.publish_checkpoint(old, "accepted-old")
    s.clock.advance(30)
    new = lease_token(await second.claim(job.job_id))
    await _save(s, old, "late-orphan")
    with pytest.raises(JobConflict):
        await first.publish_checkpoint(old, "late-orphan")
    current = await s.repo.get(job.tenant_id, job.job_id)
    assert (
        current.accepted_checkpoint_id == "accepted-old"
        and current.accepted_checkpoint_ns == old.checkpoint_ns
    )
    with pytest.raises(JobConflict):
        await second.finish(new, "waiting_approval")
    await _save(s, new, "accepted-new")
    await second.publish_checkpoint(new, "accepted-new")
    assert (
        await second.finish(new, "waiting_approval")
    ).accepted_checkpoint_ns == new.checkpoint_ns


@pytest.mark.asyncio
async def test_attempt_budget_and_absolute_deadline_limit_claims(leases):
    s = leases
    job = await s.add(max_attempts=1, timeout_at=s.clock.now() + timedelta(seconds=10))
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    assert claimed.lease_expires_at == job.timeout_at
    s.clock.advance(9)
    assert (await worker.heartbeat(lease_token(claimed))).lease_expires_at == job.timeout_at
    s.clock.advance(1)
    with pytest.raises(JobConflict):
        await worker.finish(lease_token(claimed), "succeeded")
    with pytest.raises(JobConflict):
        await s.service("other").claim(job.job_id)
    # Budget exhaustion also rejects before the absolute deadline.
    other = await s.add(max_attempts=1)
    await worker.claim(other.job_id)
    s.clock.advance(30)
    with pytest.raises(JobConflict):
        await s.service("other").claim(other.job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("remap", ["actor", "tenant", "attributes", "removed"])
async def test_current_directory_cannot_be_replaced_by_stored_claims(leases, remap):
    s = leases
    job = await s.add()
    owner = s.owner
    if remap == "actor":
        owner = owner.model_copy(
            update={"actor": owner.actor.model_copy(update={"subject_id": "peer"})}
        )
    elif remap == "tenant":
        owner = owner.model_copy(
            update={
                "actor": owner.actor.model_copy(update={"tenant_id": "foreign"}),
                "executor": owner.executor.model_copy(update={"tenant_id": "foreign"}),
            }
        )
    elif remap == "attributes":
        owner = owner.model_copy(update={"actor": owner.actor.model_copy(update={"roles": ()})})
    s.path.write_text(
        IdentityDirectory(bindings=() if remap == "removed" else (owner,)).model_dump_json()
    )
    with pytest.raises(PermissionError):
        await s.service().claim(job.job_id)
    assert await s.repo.get(job.tenant_id, job.job_id) == job


@pytest.mark.asyncio
async def test_api_executor_and_untrusted_worker_cannot_claim(leases):
    s = leases
    job = await s.add()
    with pytest.raises(ValueError, match="job_worker"):
        s.service(executor=s.owner.executor)
    impostor = s.worker.model_copy(update={"subject_id": "untrusted-worker"})
    with pytest.raises(PermissionError):
        await s.service(executor=impostor).claim(job.job_id)
    assert await s.repo.get(job.tenant_id, job.job_id) == job


@pytest.mark.asyncio
async def test_worker_cannot_reuse_the_api_executor_identifier(leases, monkeypatch):
    s = leases
    job = await s.add()
    api_as_worker = s.worker.model_copy(update={"subject_id": s.owner.executor.subject_id})
    # Even a trusted role assignment cannot collapse API and worker identities.
    monkeypatch.setattr(
        s.runtime.policy,
        "_trusted_executors",
        frozenset(
            {
                s.owner.executor,
                s.worker,
                api_as_worker,
            }
        ),
    )
    with pytest.raises(AuthenticationError, match="distinct"):
        await s.service(executor=api_as_worker).claim(job.job_id)
    assert await s.repo.get(job.tenant_id, job.job_id) == job


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_claim_rechecks_existing_campaign_business_permission(leases, allowed):
    s = leases
    job = await s.add(scenario="campaign", actor=s.admin if allowed else s.owner)
    if allowed:
        assert (await s.service().claim(job.job_id)).status == "running"
    else:
        with pytest.raises(PermissionError):
            await s.service().claim(job.job_id)
        assert await s.repo.get(job.tenant_id, job.job_id) == job


@pytest.mark.asyncio
async def test_lease_state_survives_independent_repository_process(leases):
    s = leases
    job = await s.add()
    claimed = await s.service().claim(job.job_id)
    script = """
import asyncio, json, sys
from pathlib import Path
from oria.config import resolve_runtime_config
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository
async def main():
    config = resolve_runtime_config(environ={}, data_dir=Path(sys.argv[1]))
    async with DatabaseResources(config) as db:
        job = await JobRepository(db.platform_sessions).get('tenant', sys.argv[2])
        print(json.dumps({'status': job.status, 'epoch': job.lease_epoch, 'attempt': job.attempt}))
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(s.config.data_paths.root), job.job_id],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == {"status": "running", "epoch": 1, "attempt": 1}
    assert (await s.service().heartbeat(lease_token(claimed))).lease_epoch == 1


@pytest.mark.asyncio
async def test_normal_wait_continuations_do_not_consume_failure_attempts(leases):
    s = leases
    job = await s.add(max_attempts=1)
    worker = s.service()
    for epoch in range(1, 5):
        running = await worker.claim(job.job_id)
        assert running.attempt == 1 and running.lease_epoch == epoch
        token = lease_token(running)
        await _save(s, token, f"checkpoint-{epoch}")
        await worker.publish_checkpoint(token, f"checkpoint-{epoch}")
        waiting = await worker.finish(token, "waiting_approval")
        # Fixture for a future authorized approval/inbox resolver, not Graph resume.
        queued = Job.model_validate(
            waiting.model_dump()
            | {
                "status": "queued",
                "revision": waiting.revision + 1,
            }
        )
        await s.repo.replace(waiting, queued, _trusted_guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["instance", "tenant", "epoch"])
async def test_a_token_cannot_be_reused_in_another_worker_scope(leases, change):
    s = leases
    job = await s.add()
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    target = worker
    if change == "instance":
        target = s.service("other-instance")
    elif change == "tenant":
        token = token.model_copy(update={"tenant_id": "foreign"})
    else:
        token = token.model_copy(update={"epoch": token.epoch + 1})
    with pytest.raises((PermissionError, JobConflict)):
        await target.heartbeat(token)
    assert len(await _events(s, job.job_id)) == 2


@pytest.mark.asyncio
async def test_requester_revocation_after_saver_read_cannot_publish_pointer(leases, monkeypatch):
    s = leases
    job = await s.add()
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    token = lease_token(claimed)
    await _save(s, token, "checkpoint")
    read = s.saver.aget_tuple

    async def revoke(config):
        result = await read(config)
        s.path.write_text('{"bindings": []}')
        return result

    monkeypatch.setattr(s.saver, "aget_tuple", revoke)
    with pytest.raises(AuthenticationError):
        await worker.publish_checkpoint(token, "checkpoint")
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed


@pytest.mark.asyncio
async def test_event_write_error_rolls_back_completion_and_hides_sql(leases, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from oria.core.jobs import JobUnavailable

    s = leases
    job = await s.add()
    worker = s.service()
    claimed = await worker.claim(job.job_id)

    async def fail(session, value):
        raise SQLAlchemyError("private-sql-and-parameters")

    monkeypatch.setattr(s.repo, "_append_event", fail)
    with pytest.raises(JobUnavailable) as error:
        await worker.finish(lease_token(claimed), "succeeded")
    assert "private-sql" not in str(error.value)
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed
    assert len(await _events(s, job.job_id)) == 2


@pytest.mark.asyncio
async def test_two_independent_repository_processes_only_claim_once(leases):
    s = leases
    job = await s.add()
    script = """
import asyncio, json, sys
from datetime import datetime, timedelta
from pathlib import Path
from oria.config import resolve_runtime_config
from oria.core.jobs import Job, JobConflict
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository
async def guard(): pass
async def main():
    config = resolve_runtime_config(environ={}, data_dir=Path(sys.argv[1]))
    now = datetime.fromisoformat(sys.argv[4])
    async with DatabaseResources(config) as db:
        repo = JobRepository(db.platform_sessions)
        expected = await repo.get('tenant', sys.argv[2])
        replacement = Job.model_validate(expected.model_dump() | {
            'status': 'running', 'revision': expected.revision + 1,
            'attempt': 1, 'lease_epoch': expected.lease_epoch + 1,
            'lease_owner': sys.argv[3], 'heartbeat_at': now,
            'lease_expires_at': now + timedelta(seconds=30), 'updated_at': now,
        })
        try:
            await repo.replace_leased(expected, replacement, guard,
                operation='claim', token=None, clock=lambda: now, commit_guard=lambda: None)
            print(json.dumps({'claimed': True}))
        except JobConflict:
            print(json.dumps({'claimed': False}))
asyncio.run(main())
"""

    async def run(owner):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-c",
                script,
                str(s.config.data_paths.root),
                job.job_id,
                owner,
                s.clock.now().isoformat(),
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)["claimed"]

    assert sorted(await asyncio.gather(run("process-a"), run("process-b"))) == [False, True]
    current = await s.repo.get(job.tenant_id, job.job_id)
    assert current.attempt == current.lease_epoch == 1
    assert len(await _events(s, job.job_id)) == 2
