"""Lease-fenced launch requests using real temporary SQLite approval/saga records."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from tests.integration.test_v06_job_execution_fencing import _snapshot, _spec
from tests.integration.test_v06_job_leases import leases as leases

from oria.core.approvals import ApprovalService
from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict
from oria.domain.launch import (
    DefaultCampaignLaunchService,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.orchestrator.job_execution import JobExecutionService
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import SQLiteCampaignLaunchRepository

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


@pytest_asyncio.fixture
async def launch(leases):
    s = leases
    job = await s.add(scenario="campaign", actor=s.admin)
    worker = s.service()
    claimed = await worker.claim(job.job_id)
    token = lease_token(claimed)
    service = JobExecutionService(worker, s.db)
    draft = await service.persist_campaign_draft(token, _spec(job), _snapshot(s))
    args = MaterializeCouponBatchArgs(
        campaign_id=draft.campaign.campaign_id,
        coupon_batch_id=draft.coupon_batch.coupon_batch_id,
        coupon_spec_hash=draft.coupon_batch.coupon_spec_hash,
    )
    pub = draft.recruitment_publication
    plan = DefaultCampaignLaunchService.build_launch_plan(
        draft=draft,
        materialize_args=args,
        publish_args=PublishRecruitmentArgs(
            campaign_id=args.campaign_id,
            recruitment_publication_id=pub.recruitment_publication_id,
            merchant_scope_hash=pub.merchant_scope_hash,
            material_version=pub.material_version,
        ),
    )
    ctx, _ = await worker._authorize(claimed, business_resource_id=args.campaign_id)
    return SimpleNamespace(
        s=s,
        job=job,
        claimed=claimed,
        worker=worker,
        token=token,
        service=service,
        draft=draft,
        args=args,
        plan=plan,
        ctx=ctx,
        approvals=SQLiteApprovalRepository(s.db.platform_sessions),
        launches=SQLiteCampaignLaunchRepository(s.db.business_sessions),
    )


async def _counts(c):
    async with c.s.db.platform_sessions() as session:
        approval = (await session.execute(text("SELECT count(*) FROM approvals"))).scalar_one()
        audit = (
            await session.execute(
                text("SELECT count(*) FROM audit_events WHERE action = 'approval.created'")
            )
        ).scalar_one()
        outbox = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox o JOIN audit_events a ON o.event_id = a.event_id "
                    "AND o.tenant_id = a.tenant_id WHERE a.action = 'approval.created'"
                )
            )
        ).scalar_one()
    async with c.s.db.business_sessions() as session:
        saga = (await session.execute(text("SELECT count(*) FROM launch_saga_states"))).scalar_one()
        tools = (await session.execute(text("SELECT count(*) FROM tool_executions"))).scalar_one()
    return approval, audit, outbox, saga, tools


@pytest.mark.asyncio
async def test_request_decide_and_reserve_from_persisted_job(launch, monkeypatch):
    c = launch
    result = await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert result.plan == c.plan
    approval = result.approval
    assert approval.status == "pending" and approval.decider is None
    assert approval.requester == c.job.requester_subject_id
    assert approval.canonical_args_hash == c.plan.plan_hash
    assert approval.expires_at == c.s.clock.now() + timedelta(hours=1)
    assert await c.approvals.get(c.job.tenant_id, approval.approval_id) == approval
    saga = await c.launches.get_saga(c.args.campaign_id, c.ctx)
    assert saga.status == "planned" and saga.checkpoint == approval.checkpoint_id
    assert await _counts(c) == (1, 1, 1, 1, 1)

    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == c.claimed
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, approval.approval_id
        )
    approver = c.s.admin.actor.model_copy(
        update={"subject_id": "independent-approver", "roles": ("launch_approver",)}
    )
    policy = c.s.runtime.policy
    monkeypatch.setattr(policy, "_trusted_actors", policy._trusted_actors | {approver})
    approvals = ApprovalService(c.approvals, policy, clock=c.s.clock.now)
    with pytest.raises(PermissionError):
        await approvals.decide(
            tenant_id=c.job.tenant_id,
            approval_id=approval.approval_id,
            decision="approve",
            reason=None,
            ctx=c.ctx,
        )
    decided = await approvals.decide(
        tenant_id=c.job.tenant_id,
        approval_id=approval.approval_id,
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
    assert decided.status == "approved"
    assert (await c.service.request_launch_approval(c.token, c.args, c.plan)).approval == decided
    reservation = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, approval.approval_id
    )
    assert reservation.status == "reserved" and reservation.receipt_id is None
    assert await _counts(c) == (1, 1, 1, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["campaign:launch:request", "approval:launch:request"])
async def test_request_requires_each_actual_resource_policy(launch, monkeypatch, action):
    c = launch
    original = c.s.runtime.policy.evaluate_now
    observed = []

    def deny(request, ctx):
        decision = original(request, ctx)
        if request.action == action:
            observed.append(request.resource)
            return decision.model_copy(update={"allow": False})
        return decision

    monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", deny)
    with pytest.raises(PermissionError):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert observed
    expected = (
        c.args.campaign_id
        if action.startswith("campaign")
        else (f"job:{c.job.job_id}:launch-approval:v1")
    )
    assert all(resource.resource_id == expected for resource in observed)
    assert await _counts(c) == (0, 0, 0, 0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["campaign", "coupon", "plan", "publication", "receipt"])
async def test_request_rejects_changed_plan_or_persisted_facts(launch, mutation):
    c = launch
    args, plan = c.args, c.plan
    if mutation in {"campaign", "coupon"}:
        args = args.model_copy(
            update={f"{mutation}_id" if mutation == "campaign" else ("coupon_batch_id"): "other"}
        )
    elif mutation == "plan":
        from oria.domain.ledger import LaunchPlan

        values = plan.model_dump(exclude={"plan_hash", "child_steps"}) | {
            "material_version": "changed",
            "child_steps": plan.child_steps,
        }
        plan = LaunchPlan(**values, plan_hash=LaunchPlan.compute_plan_hash(**values))
    else:
        statement = (
            "UPDATE recruitment_publications SET version = version + 1"
            if mutation == "publication"
            else "UPDATE tool_executions SET receipt_summary_hash = :hash"
        )
        async with c.s.db.business_sessions.begin() as session:
            await session.execute(text(statement), {"hash": "sha256:" + "0" * 64})
    with pytest.raises((PermissionError, ValueError)):
        await c.service.request_launch_approval(c.token, args, plan)
    assert await _counts(c) == (0, 0, 0, 0, 1)


@pytest.mark.asyncio
async def test_approval_expiry_is_capped_by_job_timeout(launch):
    c = launch
    c.s.clock.advance(86400 - 20)
    replacement = c.s.service("replacement")
    token = lease_token(await replacement.claim(c.job.job_id))
    result = await JobExecutionService(replacement, c.s.db).request_launch_approval(
        token, c.args, c.plan
    )
    assert result.approval.expires_at == c.job.timeout_at


@pytest.mark.asyncio
async def test_saga_commit_gap_can_be_repaired_without_duplicate_approval(launch, monkeypatch):
    c = launch
    writer = c.service._coupon_writer
    original = writer.require_launch

    async def fail(*args):
        raise RuntimeError("synthetic phase-two interruption")

    monkeypatch.setattr(writer, "require_launch", fail)
    with pytest.raises(RuntimeError, match="phase-two interruption"):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (0, 0, 0, 1, 1)
    monkeypatch.setattr(writer, "require_launch", original)
    first = await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await c.service.request_launch_approval(c.token, c.args, c.plan) == first
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_platform_approval_audit_outbox_roll_back_together(launch, monkeypatch):
    c = launch
    original = c.service._approvals._add

    async def fail(*args):
        await original(*args)
        raise RuntimeError("synthetic approval write interruption")

    monkeypatch.setattr(c.service._approvals, "_add", fail)
    with pytest.raises(RuntimeError, match="approval write interruption"):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (0, 0, 0, 1, 1)
    monkeypatch.setattr(c.service._approvals, "_add", original)
    await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_committed_approval_survives_lost_response(launch, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    c = launch
    original = AsyncSession.commit

    async def lose_response(session):
        await original(session)
        if session.bind is c.s.db.platform_sessions.kw["bind"]:
            raise RuntimeError("synthetic lost commit response")

    monkeypatch.setattr(AsyncSession, "commit", lose_response)
    with pytest.raises(RuntimeError, match="lost commit response"):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (1, 1, 1, 1, 1)
    monkeypatch.setattr(AsyncSession, "commit", original)
    first = await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert first.approval.status == "pending"
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["saga", "approval"])
@pytest.mark.parametrize("change", ["expiry", "directory", "policy", "cancel_task"])
async def test_await_boundary_rolls_back_and_releases_locks(launch, monkeypatch, stage, change):
    import asyncio

    c = launch
    owner = c.service._coupon_writer if stage == "saga" else c.service._approvals
    method = "ensure_launch" if stage == "saga" else "_add"
    original = getattr(owner, method)
    before_directory = c.s.path.read_text()
    policy = c.s.runtime.policy
    before_policy = policy.evaluate_now

    async def intercept(*args):
        result = await original(*args)
        if change == "expiry":
            c.s.clock.advance(30)
        elif change == "directory":
            c.s.path.write_text('{"bindings": []}')
        elif change == "policy":

            def deny(request, ctx):
                decision = before_policy(request, ctx)
                return decision.model_copy(update={"allow": False})

            monkeypatch.setattr(policy, "evaluate_now", deny)
        else:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(owner, method, intercept)
    with pytest.raises((JobConflict, PermissionError, asyncio.CancelledError)):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (0, 0, 0, int(stage == "approval"), 1)
    monkeypatch.setattr(owner, method, original)
    monkeypatch.setattr(policy, "evaluate_now", before_policy)
    c.s.path.write_text(before_directory)
    if change == "expiry":
        replacement = c.s.service("replacement")
        token = lease_token(await replacement.claim(c.job.job_id))
        service = JobExecutionService(replacement, c.s.db)
    else:
        service, token = c.service, c.token
    await service.request_launch_approval(token, c.args, c.plan)
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_args_hash", "sha256:" + "0" * 64),
        ("checkpoint_id", "other-checkpoint"),
        ("policy_version", "other-policy"),
        ("requester", "other-requester"),
        ("tool_name", "other-tool"),
        ("approval_action", "consumer_publish_approval"),
        ("status", "invalidated"),
    ],
)
async def test_conflicting_approval_history_is_never_recreated(launch, field, value):
    c = launch
    first = await c.service.request_launch_approval(c.token, c.args, c.plan)
    # Trusted DB corruption fixture; normal Repository.replace forbids binding changes.
    assert field in {
        "canonical_args_hash",
        "checkpoint_id",
        "policy_version",
        "requester",
        "tool_name",
        "approval_action",
        "status",
    }
    async with c.s.db.platform_sessions.begin() as session:
        await session.execute(text(f"UPDATE approvals SET {field} = :value"), {"value": value})
    with pytest.raises(PermissionError):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    current = await c.approvals.get(c.job.tenant_id, first.approval.approval_id)
    assert getattr(current, field) == value
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_expired_approval_history_is_not_extended(launch):
    c = launch
    first = await c.service.request_launch_approval(c.token, c.args, c.plan)
    c.s.clock.advance(3600)
    replacement = c.s.service("replacement")
    token = lease_token(await replacement.claim(c.job.job_id))
    with pytest.raises(PermissionError):
        await JobExecutionService(replacement, c.s.db).request_launch_approval(
            token, c.args, c.plan
        )
    assert await c.approvals.get(c.job.tenant_id, first.approval.approval_id) == first.approval
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_repeat_and_new_epoch_keep_one_approval_and_expiry(launch):
    c = launch
    first = await c.service.request_launch_approval(c.token, c.args, c.plan)
    c.s.clock.advance(5)
    assert await c.service.request_launch_approval(c.token, c.args, c.plan) == first
    c.s.clock.advance(25)
    replacement = c.s.service("replacement")
    token = lease_token(await replacement.claim(c.job.job_id))
    with pytest.raises(JobConflict):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert (
        await JobExecutionService(replacement, c.s.db).request_launch_approval(
            token, c.args, c.plan
        )
        == first
    )
    assert await _counts(c) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["saga", "approval"])
@pytest.mark.parametrize("change", ["expiry", "policy"])
async def test_commit_entry_checks_current_authority(launch, monkeypatch, stage, change):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    c = launch
    target = c.s.db.business_sessions if stage == "saga" else c.s.db.platform_sessions
    original = c.s.runtime.policy.evaluate_now
    reached = []
    if stage == "approval":
        add = c.service._approvals._add

        async def mark_approval_transaction(session, *args):
            await add(session, *args)
            session.sync_session.info["test_launch_approval_written"] = True

        monkeypatch.setattr(c.service._approvals, "_add", mark_approval_transaction)

    def mutate(session):
        if session.bind is not target.kw["bind"].sync_engine:
            return
        if stage == "approval" and not session.info.get("test_launch_approval_written"):
            return
        reached.append(True)
        if change == "expiry":
            c.s.clock.advance(30)
        else:

            def deny(request, ctx):
                return original(request, ctx).model_copy(update={"allow": False})

            monkeypatch.setattr(c.s.runtime.policy, "evaluate_now", deny)

    event.listen(Session, "before_commit", mutate)
    try:
        with pytest.raises((PermissionError, JobConflict)):
            await c.service.request_launch_approval(c.token, c.args, c.plan)
    finally:
        event.remove(Session, "before_commit", mutate)
    assert reached == [True]
    assert await _counts(c) == (0, 0, 0, int(stage == "approval"), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["draft", "saga", "cancel"])
async def test_phase_two_rechecks_after_durable_saga(launch, monkeypatch, change):
    from contextlib import asynccontextmanager

    from oria.api.jobs import JobAPI

    c = launch
    original = c.s.repo._hold_execution_lease
    calls = 0

    @asynccontextmanager
    async def between_phases(*args):
        nonlocal calls
        calls += 1
        async with original(*args) as platform:
            yield platform
        if calls == 1:
            if change == "cancel":
                await JobAPI(c.s.runtime, c.s.repo, c.s.source, clock=c.s.clock.now).cancel(
                    c.job.job_id, c.s.admin, "synthetic-between-phases"
                )
            else:
                statement = (
                    "UPDATE recruitment_publications SET version = version + 1"
                    if change == "draft"
                    else "UPDATE launch_saga_states SET version = version + 1"
                )
                async with c.s.db.business_sessions.begin() as session:
                    await session.execute(text(statement))

    monkeypatch.setattr(c.s.repo, "_hold_execution_lease", between_phases)
    with pytest.raises((PermissionError, JobConflict)):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == (0, 0, 0, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "cancel", "identity"])
@pytest.mark.parametrize("history", [False, True])
async def test_invalid_authority_cannot_reuse_or_create_approval(launch, change, history):
    from oria.api.jobs import JobAPI

    c = launch
    if history:
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    before = await _counts(c)
    if change == "expiry":
        c.s.clock.advance(30)
    elif change == "cancel":
        await JobAPI(c.s.runtime, c.s.repo, c.s.source, clock=c.s.clock.now).cancel(
            c.job.job_id, c.s.admin, "synthetic-invalid-authority"
        )
    else:
        c.s.path.write_text('{"bindings": []}')
    with pytest.raises((PermissionError, JobConflict)):
        await c.service.request_launch_approval(c.token, c.args, c.plan)
    assert await _counts(c) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("contender", ["cancel", "takeover", "business"])
async def test_two_resources_cannot_cross_approval_commit_locks(launch, monkeypatch, contender):
    import asyncio

    from sqlalchemy import event

    from oria.api.jobs import JobAPI
    from oria.orchestrator.job_leases import JobLeaseService
    from oria.storage.database import DatabaseResources
    from oria.storage.jobs import JobRepository

    c = launch
    s = c.s
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = c.service._approvals._add

    async def pause(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(c.service._approvals, "_add", pause)
    async with DatabaseResources(s.config) as other:
        repo = JobRepository(other.platform_sessions)
        if contender == "cancel":
            api = JobAPI(s.runtime, repo, s.source, clock=s.clock.now)
            ctx = api._context(s.admin, "synthetic-approval-race", c.claimed)
            guard = await api._authorize(
                s.admin, ctx, (api._request("job:cancel", ctx, c.claimed),)
            )

            async def preauthorized(*args):
                return guard

            monkeypatch.setattr(api, "_authorize", preauthorized)
        elif contender == "takeover":
            replacement = JobLeaseService(
                s.runtime,
                repo,
                s.source,
                executor=s.worker,
                instance_id="other-resource",
                clock=s.clock.now,
            )
            authorize = replacement._authorize
            authorization = await authorize(c.claimed)

            async def preauthorized(job, **kwargs):
                assert job == c.claimed
                return authorization

            monkeypatch.setattr(replacement, "_authorize", preauthorized)

        async def change_business():
            async with other.business_sessions.begin() as session:
                await session.execute(
                    text("UPDATE recruitment_publications SET version = version + 1")
                )

        engine = other._business_engine if contender == "business" else other._platform_engine
        prefix = (
            "UPDATE recruitment_publications SET" if contender == "business" else "UPDATE jobs SET"
        )

        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith(prefix):
                attempted.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        first = asyncio.create_task(c.service.request_launch_approval(c.token, c.args, c.plan))
        competing = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if contender == "business":
                operation = change_business()
            elif contender == "cancel":
                operation = api.cancel(c.job.job_id, s.admin, "synthetic-approval-race")
            else:
                s.clock.advance(30)
                operation = replacement.claim(c.job.job_id)
            competing = asyncio.create_task(operation)
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(competing), 0.1)
            assert await _counts(c) == (0, 0, 0, 1, 1)
            release.set()
            if contender == "takeover":
                with pytest.raises(JobConflict):
                    await asyncio.wait_for(first, 5)
                takeover = await asyncio.wait_for(competing, 5)
                assert await _counts(c) == (0, 0, 0, 1, 1)
                monkeypatch.setattr(replacement, "_authorize", authorize)
                await JobExecutionService(replacement, other).request_launch_approval(
                    lease_token(takeover), c.args, c.plan
                )
            else:
                assert (await asyncio.wait_for(first, 5)).approval.status == "pending"
                await asyncio.wait_for(competing, 5)
            assert await _counts(c) == (1, 1, 1, 1, 1)
            with pytest.raises((PermissionError, JobConflict)):
                await c.service.request_launch_approval(c.token, c.args, c.plan)
        finally:
            release.set()
            pending = [task for task in (first, competing) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
