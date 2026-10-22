"""Opt-in local draft commits under a real SQLite Job fence; no runner/adapter."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from tests.integration.test_v06_job_leases import _trusted_guard
from tests.integration.test_v06_job_leases import leases as leases
from tests.support.launch import snapshot

from oria.core.job_leases import lease_token
from oria.core.jobs import Job, JobConflict
from oria.domain.launch import CampaignDraftSpec

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


async def _counts(s):
    tables = (
        "campaign_rule_snapshot_refs",
        "campaigns",
        "coupon_batches",
        "recruitment_publications",
        "tool_executions",
        "domain_events",
        "audit_events",
        "outbox",
    )
    async with s.db.business_sessions() as session:
        return tuple(
            [
                (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                for table in tables
            ]
        )


def _spec(job):
    suffix = job.job_id.removeprefix("job_")
    return CampaignDraftSpec(
        campaign_id=f"campaign_{suffix}",
        coupon_batch_id=f"coupon_{suffix}",
        recruitment_publication_id=f"recruitment_{suffix}",
        material_version="material-v1",
        compensation_policy_version="compensation-v1",
    )


def _snapshot(s):
    value = snapshot()
    value = value.model_copy(
        update={
            "tenant_id": s.admin.actor.tenant_id,
            "enrollment_policy": value.enrollment_policy.model_copy(update={"mode": "auto"}),
        }
    )
    return value.model_copy(update={"snapshot_hash": value.recompute_hash()})


@pytest.mark.asyncio
async def test_cancelled_job_cannot_commit_domain_or_reservation(leases):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    cancelled = Job.model_validate(
        claimed.model_dump()
        | {
            "status": "cancellation_requested",
            "cancel_requested": True,
            "revision": claimed.revision + 1,
        }
    )
    await s.repo.replace(claimed, cancelled, _trusted_guard)
    with pytest.raises(JobConflict):
        await JobExecutionService(worker, s.db).persist_campaign_draft(
            lease_token(claimed), _spec(job), _snapshot(s)
        )
    assert await _counts(s) == (0,) * 8


@pytest.mark.asyncio
async def test_valid_draft_and_reservation_commit_once_and_survive_new_epoch(leases):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    service = JobExecutionService(worker, s.db)
    token = lease_token(claimed)
    draft = await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await service.persist_campaign_draft(token, _spec(job), _snapshot(s)) == draft
    assert await _counts(s) == (1,) * 8
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed
    s.clock.advance(30)
    replacement = s.service("replacement")
    new = lease_token(await replacement.claim(job.job_id))
    assert (
        await JobExecutionService(replacement, s.db).persist_campaign_draft(
            new, _spec(job), _snapshot(s)
        )
        == draft
    )
    assert await _counts(s) == (1,) * 8
    async with s.db.business_sessions() as session:
        row = (
            await session.execute(
                text("SELECT status, attempt_count, checkpoint_id FROM tool_executions")
            )
        ).one()
    assert row == ("succeeded", 1, f"{token.checkpoint_ns}:campaign-draft")


def _revoke(s, monkeypatch, kind):
    if kind == "identity":
        s.path.write_text('{"bindings": []}')
    elif kind == "actor_policy":
        monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())
    elif kind == "worker_policy":
        monkeypatch.setattr(s.runtime.policy, "_trusted_executors", frozenset())
    elif kind == "expiry":
        s.clock.advance(30)
    else:
        raise AssertionError(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["identity", "actor_policy", "worker_policy", "expiry"])
async def test_current_authority_rechecked_at_commit_entry(leases, monkeypatch, kind):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    service = JobExecutionService(worker, s.db)
    reached = []

    def revoke_at_commit(session):
        if session.bind.url.database == str(s.config.data_paths.business_db):
            reached.append(True)
            _revoke(s, monkeypatch, kind)

    event.listen(Session, "before_commit", revoke_at_commit)
    try:
        with pytest.raises((PermissionError, JobConflict)):
            await service.persist_campaign_draft(lease_token(claimed), _spec(job), _snapshot(s))
    finally:
        event.remove(Session, "before_commit", revoke_at_commit)
    assert reached == [True]
    assert await _counts(s) == (0,) * 8
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["identity", "actor_policy", "worker_policy", "expiry"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_authority_revoked_across_business_await_rolls_back(
    leases, monkeypatch, kind, duplicate
):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    service = JobExecutionService(worker, s.db)
    token = lease_token(claimed)
    if duplicate:
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    persist = service._writer.persist

    async def revoke(*args):
        await persist(*args)
        _revoke(s, monkeypatch, kind)

    monkeypatch.setattr(service._writer, "persist", revoke)
    with pytest.raises((PermissionError, JobConflict)):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == ((1,) * 8 if duplicate else (0,) * 8)
    if kind.endswith("policy"):
        async with s.db.platform_sessions() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE action='job:execute' AND decision='deny'"
                    )
                )
            ).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reservation", "campaign", "domain", "audit", "outbox"])
async def test_failure_after_business_insert_rolls_back_entire_bundle(leases, monkeypatch, stage):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    target, method = {
        "reservation": (service._writer._tools, "_reserve_in_session"),
        "campaign": (service._writer._drafts._campaigns, "_insert"),
        "domain": (service._writer._events, "_append"),
        "audit": (service._writer._audit, "_append"),
        "outbox": (service._writer._outbox, "_append"),
    }[stage]
    original = getattr(target, method)

    async def fail(*args):
        await original(*args)
        raise RuntimeError("injected local persistence failure")

    monkeypatch.setattr(target, method, fail)
    with pytest.raises(RuntimeError, match="injected"):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == (0,) * 8
    await worker.heartbeat(token)  # Both database transactions and platform lock were released.


@pytest.mark.asyncio
async def test_task_cancellation_rolls_back_reservation_domain_and_outbox(leases, monkeypatch):
    import asyncio

    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    entered, resume = asyncio.Event(), asyncio.Event()
    append = service._writer._outbox._append

    async def pause(*args):
        await append(*args)
        entered.set()
        await resume.wait()

    monkeypatch.setattr(service._writer._outbox, "_append", pause)
    task = asyncio.create_task(service.persist_campaign_draft(token, _spec(job), _snapshot(s)))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _counts(s) == (0,) * 8
    await worker.heartbeat(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "job",
        "worker",
        "scenario",
        "campaign",
        "coupon",
        "publication",
        "mode",
        "snapshot_tenant",
        "untrusted",
    ],
)
async def test_job_tenant_and_command_binding_fail_closed(leases, change):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="attribution" if change == "scenario" else "campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    spec, rules = _spec(job), _snapshot(s)
    if change == "tenant":
        token = token.model_copy(update={"tenant_id": "another-tenant"})
    elif change == "job":
        other = await s.add(scenario="campaign", actor=s.admin)
        token = token.model_copy(update={"job_id": other.job_id})
    elif change == "worker":
        worker = s.service("another-instance")
    elif change in {"campaign", "coupon", "publication"}:
        field = {
            "campaign": "campaign_id",
            "coupon": "coupon_batch_id",
            "publication": "recruitment_publication_id",
        }[change]
        spec = spec.model_copy(update={field: "unrelated-business-resource"})
    elif change in {"mode", "snapshot_tenant"}:
        rules = rules.model_copy(
            update=(
                {"enrollment_policy": rules.enrollment_policy.model_copy(update={"mode": "hybrid"})}
                if change == "mode"
                else {"tenant_id": "another-tenant"}
            )
        )
        rules = rules.model_copy(update={"snapshot_hash": rules.recompute_hash()})
    elif change == "untrusted":
        # Same owner derivation and token cannot authorize an untrusted executor value.
        worker = s.service(executor=s.worker.model_copy(update={"authn_method": "untrusted"}))
    with pytest.raises((PermissionError, JobConflict, ValueError)):
        await JobExecutionService(worker, s.db).persist_campaign_draft(token, spec, rules)
    assert await _counts(s) == (0,) * 8


@pytest.mark.asyncio
async def test_expired_and_reclaimed_epoch_cannot_write_or_reuse_history(leases):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    s.clock.advance(30)
    with pytest.raises(JobConflict):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    second = s.service("replacement")
    new = lease_token(await second.claim(job.job_id))
    with pytest.raises(JobConflict):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    await JobExecutionService(second, s.db).persist_campaign_draft(new, _spec(job), _snapshot(s))
    with pytest.raises(JobConflict):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == (1,) * 8


@pytest.mark.asyncio
async def test_idempotency_payload_conflict_preserves_committed_bundle(leases):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    changed = _spec(job).model_copy(update={"material_version": "changed-material"})
    with pytest.raises(ValueError, match="conflicts"):
        await service.persist_campaign_draft(token, changed, _snapshot(s))
    assert await _counts(s) == (1,) * 8


@pytest.mark.asyncio
@pytest.mark.parametrize("contender", ["takeover", "cancel"])
async def test_independent_sqlite_instances_cannot_cross_held_business_commit(
    leases, monkeypatch, contender
):
    import asyncio

    from sqlalchemy import event

    from oria.api.jobs import JobAPI
    from oria.orchestrator.job_execution import JobExecutionService
    from oria.orchestrator.job_leases import JobLeaseService
    from oria.storage.database import DatabaseResources
    from oria.storage.jobs import JobRepository

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    first = s.service()
    claimed = await first.claim(job.job_id)
    token = lease_token(claimed)
    service = JobExecutionService(first, s.db)
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    append = service._writer._outbox._append

    async def pause(*args):
        await append(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(service._writer._outbox, "_append", pause)
    async with DatabaseResources(s.config) as other:
        repository = JobRepository(other.platform_sessions)
        second = JobLeaseService(
            s.runtime,
            repository,
            s.source,
            executor=s.worker,
            instance_id="independent",
            clock=s.clock.now,
        )
        # Preauthorize using real directory/Policy before holding the lock, so the race
        # reaches the actual CAS UPDATE rather than blocking on its earlier audit write.
        # The returned real guard still rechecks identity/Policy within the CAS transaction.
        if contender == "takeover":
            original_authorize = second._authorize
            authorized = await original_authorize(claimed)

            async def preauthorized(value, **kwargs):
                assert value == claimed
                return authorized

            monkeypatch.setattr(second, "_authorize", preauthorized)
        else:
            api = JobAPI(s.runtime, repository, s.source, clock=s.clock.now)
            ctx = api._context(s.admin, "cancel-race", claimed)
            cancel_guard = await api._authorize(
                s.admin, ctx, (api._request("job:cancel", ctx, claimed),)
            )

            async def preauthorized(*args):
                return cancel_guard

            monkeypatch.setattr(api, "_authorize", preauthorized)

        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE jobs SET"):
                attempted.set()

        event.listen(other._platform_engine.sync_engine, "before_cursor_execute", observe)
        first_task = asyncio.create_task(
            service.persist_campaign_draft(token, _spec(job), _snapshot(s))
        )
        await asyncio.wait_for(entered.wait(), 5)
        if contender == "takeover":
            s.clock.advance(30)
            competing = asyncio.create_task(second.claim(job.job_id))
        else:
            competing = asyncio.create_task(api.cancel(job.job_id, s.admin, "cancel-race"))
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(competing), 0.1)
            assert await _counts(s) == (0,) * 8  # Uncommitted business facts are invisible.
            release.set()
            if contender == "takeover":
                with pytest.raises(JobConflict):
                    await first_task
                new = await asyncio.wait_for(competing, 5)
                assert new.lease_epoch == token.epoch + 1
                assert await _counts(s) == (0,) * 8
                monkeypatch.setattr(second, "_authorize", original_authorize)
                await JobExecutionService(second, other).persist_campaign_draft(
                    lease_token(new), _spec(job), _snapshot(s)
                )
            else:
                await first_task
                cancelled = await asyncio.wait_for(competing, 5)
                assert cancelled.status == "cancellation_requested"
            assert await _counts(s) == (1,) * 8
            with pytest.raises(JobConflict):
                await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
        finally:
            release.set()
            for task in (first_task, competing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first_task, competing, return_exceptions=True)
            event.remove(other._platform_engine.sync_engine, "before_cursor_execute", observe)


@pytest.mark.asyncio
async def test_cancellation_after_authorization_before_fence_is_not_lost(leases, monkeypatch):
    from oria.api.jobs import JobAPI
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    leased = worker._leased
    api = JobAPI(s.runtime, s.repo, s.source, clock=s.clock.now)

    async def cancel_before_lock(*args, **kwargs):
        result = await leased(*args, **kwargs)
        await api.cancel(job.job_id, s.admin, "cancel-before-lock")
        return result

    monkeypatch.setattr(worker, "_leased", cancel_before_lock)
    with pytest.raises(JobConflict):
        await JobExecutionService(worker, s.db).persist_campaign_draft(
            token, _spec(job), _snapshot(s)
        )
    assert await _counts(s) == (0,) * 8


@pytest.mark.asyncio
async def test_platform_release_error_does_not_claim_rollback_of_committed_business(
    leases, monkeypatch
):
    from contextlib import asynccontextmanager

    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    token = lease_token(claimed)
    service = JobExecutionService(worker, s.db)
    hold = s.repo._hold_execution_lease

    @asynccontextmanager
    async def response_failure(*args):
        async with hold(*args):
            yield
        raise RuntimeError("injected failure after Business DB commit")

    monkeypatch.setattr(s.repo, "_hold_execution_lease", response_failure)
    with pytest.raises(RuntimeError, match="after Business DB commit"):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == (1,) * 8
    assert await s.repo.get(job.tenant_id, job.job_id) == claimed
    monkeypatch.setattr(s.repo, "_hold_execution_lease", hold)
    await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == (1,) * 8


@pytest.mark.asyncio
async def test_actual_campaign_resource_policy_rechecked_without_token_bypass(leases, monkeypatch):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    persist, evaluate = service._writer.persist, s.runtime.policy.evaluate_now
    observed = []

    def deny_campaign(request, ctx):
        decision = evaluate(request, ctx)
        if request.action == "campaign:draft:write":
            observed.append(request.resource.resource_id)
            return decision.model_copy(update={"allow": False, "constraints": {}})
        return decision

    async def revoke(*args):
        await persist(*args)
        monkeypatch.setattr(s.runtime.policy, "evaluate_now", deny_campaign)

    monkeypatch.setattr(service._writer, "persist", revoke)
    with pytest.raises(PermissionError):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert observed == [_spec(job).campaign_id]
    assert await _counts(s) == (0,) * 8
    async with s.db.platform_sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action='campaign:draft:write' AND decision='deny'"
                )
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_identity_attributes_changed_across_business_await_fail_closed(leases, monkeypatch):
    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    token = lease_token(await worker.claim(job.job_id))
    service = JobExecutionService(worker, s.db)
    persist = service._writer.persist

    async def change_attributes(*args):
        await persist(*args)
        changed = s.admin.model_copy(
            update={"actor": s.admin.actor.model_copy(update={"roles": ("operator",)})}
        )
        directory = s.directory.model_copy(update={"bindings": (s.owner, changed)})
        s.path.write_text(directory.model_dump_json())

    monkeypatch.setattr(service._writer, "persist", change_attributes)
    with pytest.raises(PermissionError):
        await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    assert await _counts(s) == (0,) * 8


@pytest.mark.asyncio
async def test_deadline_crossed_before_commit_rolls_back(leases, monkeypatch):
    from datetime import timedelta

    from oria.orchestrator.job_execution import JobExecutionService

    s = leases
    job = await s.add(
        scenario="campaign", actor=s.admin, timeout_at=s.clock.now() + timedelta(seconds=10)
    )
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    assert claimed.lease_expires_at == job.timeout_at
    service = JobExecutionService(worker, s.db)
    persist = service._writer.persist

    async def expire(*args):
        await persist(*args)
        s.clock.advance(10)

    monkeypatch.setattr(service._writer, "persist", expire)
    with pytest.raises(JobConflict):
        await service.persist_campaign_draft(lease_token(claimed), _spec(job), _snapshot(s))
    assert await _counts(s) == (0,) * 8


@pytest.mark.asyncio
async def test_fence_does_not_enable_other_backends_or_mismatched_platform_repository(leases):
    from types import SimpleNamespace

    from oria.orchestrator.job_execution import JobExecutionService
    from oria.storage.database import DatabaseResources

    s = leases
    with pytest.raises(ValueError, match="requires SQLite"):
        JobExecutionService(
            s.service(), SimpleNamespace(platform_backend="postgres", business_backend="sqlite")
        )
    async with DatabaseResources(s.config) as independent:
        with pytest.raises(ValueError, match="supplied platform sessions"):
            JobExecutionService(s.service(), independent)
