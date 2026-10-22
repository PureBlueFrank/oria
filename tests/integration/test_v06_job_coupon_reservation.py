"""Typed reservation only, with synthetic approved saga facts and temporary SQLite."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from tests.integration.test_v06_job_execution_fencing import _counts, _snapshot, _spec
from tests.integration.test_v06_job_leases import leases as leases

from oria.core.approvals import ApprovalService
from oria.core.job_leases import lease_token
from oria.domain.business import LaunchSagaState
from oria.domain.launch import (
    LAUNCH_PLAN_TOOL_NAME,
    DefaultCampaignLaunchService,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.orchestrator.job_execution import JobExecutionService
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import SQLiteCampaignLaunchRepository

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


@pytest_asyncio.fixture
async def coupon(leases, monkeypatch):
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
    publication = draft.recruitment_publication
    plan = DefaultCampaignLaunchService.build_launch_plan(
        draft=draft,
        materialize_args=args,
        publish_args=PublishRecruitmentArgs(
            campaign_id=args.campaign_id,
            recruitment_publication_id=publication.recruitment_publication_id,
            merchant_scope_hash=publication.merchant_scope_hash,
            material_version=publication.material_version,
        ),
    )
    ctx, _ = await worker._authorize(claimed, business_resource_id=args.campaign_id)
    approver = s.admin.actor.model_copy(
        update={"subject_id": "synthetic-approver", "roles": ("launch_approver",)}
    )
    policy = s.runtime.policy
    monkeypatch.setattr(policy, "_trusted_actors", policy._trusted_actors | {approver})
    approvals = ApprovalService(
        SQLiteApprovalRepository(s.db.platform_sessions), policy, clock=s.clock.now
    )
    # Trusted preconditions only: no Job Graph, accepted pointer or adapter is invoked.
    approval = await approvals.create(
        approval_action="launch_approval",
        tool_name=LAUNCH_PLAN_TOOL_NAME,
        canonical_args_hash=plan.plan_hash,
        checkpoint_id=f"synthetic-saga:{job.job_id}",
        expires_at=s.clock.now() + timedelta(hours=1),
        ctx=ctx,
    )
    approval = await approvals.decide(
        tenant_id=job.tenant_id,
        approval_id=approval.approval_id,
        decision="approve",
        reason=None,
        ctx=s.runtime.new_context(
            actor=approver,
            executor=s.worker,
            session_id=job.session_id,
            thread_id=job.thread_id,
            run_id=job.run_id,
        ),
    )
    launches = SQLiteCampaignLaunchRepository(s.db.business_sessions)
    saga = await launches.create_saga(
        LaunchSagaState(
            launch_saga_id=f"saga:{job.job_id}",
            tenant_id=job.tenant_id,
            campaign_id=args.campaign_id,
            status="planned",
            checkpoint=approval.checkpoint_id,
            version=1,
            created_at=s.clock.now(),
            updated_at=s.clock.now(),
        ),
        ctx,
    )
    return SimpleNamespace(
        s=s,
        job=job,
        claimed=claimed,
        token=token,
        worker=worker,
        service=service,
        draft=draft,
        args=args,
        plan=plan,
        approval=approval,
        approvals=approvals,
        launches=launches,
        saga=saga,
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_valid_approved_coupon_reserves_without_dispatch(coupon, monkeypatch):
    from oria.adapters.launch import InMemoryCouponBatchAdapter
    from oria.core.execution_ledger import ExecutionLedger

    async def forbidden(*args, **kwargs):
        raise AssertionError("reservation must not dispatch or execute")

    monkeypatch.setattr(InMemoryCouponBatchAdapter, "materialize", forbidden)
    monkeypatch.setattr(DefaultCampaignLaunchService, "materialize_coupon_batch", forbidden)
    monkeypatch.setattr(ExecutionLedger, "execute", forbidden)
    c = coupon
    result = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, c.approval.approval_id
    )
    assert result.status == "reserved"
    assert result.executed_at is None and result.receipt_id is None
    assert result.checkpoint_id == c.saga.checkpoint
    assert await _counts(c.s) == (1, 1, 1, 1, 2, 1, 1, 1)
    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == c.claimed
    assert await c.launches.get_saga(c.args.campaign_id, c.ctx) == c.saga
    async with c.s.db.business_sessions() as session:
        assert (
            await session.execute(text("SELECT status FROM coupon_batches"))
        ).scalar() == "draft"


@pytest.mark.asyncio
async def test_duplicate_and_new_epoch_use_existing_ledger_key(coupon):
    from oria.core.approvals import canonical_args_hash
    from oria.core.execution_ledger import ExecutionLedger
    from oria.core.jobs import JobConflict

    c = coupon
    first = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, c.approval.approval_id
    )
    expected_hash = canonical_args_hash(
        tool_name="materialize_coupon_batch",
        tool_schema_version=1,
        schema=MaterializeCouponBatchArgs,
        args=c.args.model_dump(),
    )
    assert first.idempotency_key == ExecutionLedger.build_idempotency_key(
        f"{c.args.campaign_id}:{c.args.coupon_spec_hash}", expected_hash
    )
    assert (
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
        == first
    )
    c.s.clock.advance(30)
    second = c.s.service("replacement")
    token = lease_token(await second.claim(c.job.job_id))
    with pytest.raises(JobConflict):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert (
        await JobExecutionService(second, c.s.db).reserve_coupon_materialization(
            token, c.args, c.plan, c.approval.approval_id
        )
        == first
    )
    assert await _counts(c.s) == (1, 1, 1, 1, 2, 1, 1, 1)


@pytest.mark.asyncio
async def test_existing_ledger_cannot_supply_an_unapproved_checkpoint(coupon):
    from oria.core.execution_ledger import ExecutionLedger

    c = coupon
    await ExecutionLedger(c.s.db.business_sessions, clock=c.s.clock.now).reserve_for_args(
        execution_id="preexisting-reservation",
        tenant_id=c.job.tenant_id,
        tool_name="materialize_coupon_batch",
        tool_schema_version=1,
        schema=MaterializeCouponBatchArgs,
        args=c.args.model_dump(),
        stable_business_id=f"{c.args.campaign_id}:{c.args.coupon_spec_hash}",
        checkpoint_id="unapproved-checkpoint",
    )
    before = await _counts(c.s)
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "pending",
        "rejected",
        "expired",
        "invalidated",
        "requester",
        "action",
        "tool",
        "hash",
        "checkpoint",
        "version",
        "tenant",
        "downstream_binding",
    ],
)
async def test_invalid_approval_never_adds_reservation(coupon, monkeypatch, change):
    from oria.core.approvals import Approval, ApprovalBusinessBinding

    c = coupon
    values = c.approval.model_dump()
    approval_id = c.approval.approval_id
    if change == "missing":
        approval_id = "missing-approval"
    elif change == "invalidated":
        await c.approvals.invalidate_binding(c.approval, ctx=c.ctx)
    elif change == "tenant":
        values.update(tenant_id="other-tenant", approval_id="other-tenant-approval")
        await c.approvals._repository.add(Approval.model_validate(values))
        approval_id = values["approval_id"]
    else:
        if change == "pending":
            values.update(status="pending", decider=None, decision=None, decided_at=None)
        elif change == "rejected":
            values.update(status="rejected", decision="reject", reason="synthetic rejection")
        elif change == "expired":
            values["expires_at"] = c.s.clock.now() + timedelta(seconds=1)
            c.s.clock.advance(1)
        elif change == "requester":
            values["requester"] = "different-requester"
        elif change == "action":
            values["approval_action"] = "consumer_publish_approval"
        elif change == "tool":
            values["tool_name"] = "other-tool"
        elif change == "hash":
            values["canonical_args_hash"] = "sha256:" + "a" * 64
        elif change == "checkpoint":
            values["checkpoint_id"] = "arbitrary-untrusted-pointer"
        elif change == "version":
            values["policy_version"] = "stale-policy-version"
        elif change == "downstream_binding":
            values["business_binding"] = ApprovalBusinessBinding(
                campaign_id=c.args.campaign_id,
                enrollment_version=1,
                link_version=0,
                selection_version="none",
                rule_snapshot_hash=c.plan.rule_snapshot_hash,
            )
        # Binding metadata is immutable: prepare a distinct trusted fixture record.
        values["approval_id"] += ":changed"
        await c.approvals._repository.add(Approval.model_validate(values))
        approval_id = values["approval_id"]
    reserved = []
    original = c.service._coupon_writer._tools._reserve_in_session

    async def observe_reservation(*args):
        reserved.append(True)
        return await original(*args)

    monkeypatch.setattr(c.service._coupon_writer._tools, "_reserve_in_session", observe_reservation)
    before = await _counts(c.s)
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(c.token, c.args, c.plan, approval_id)
    assert await _counts(c.s) == before
    assert reserved == []  # Initial approval denial occurs BEFORE ledger reservation.


def _changed_plan(plan, **updates):
    from oria.domain.ledger import LaunchPlan

    fields = plan.model_dump() | updates
    fields.pop("plan_hash")
    fields["child_steps"] = updates.get("child_steps", plan.child_steps)
    return LaunchPlan(**fields, plan_hash=LaunchPlan.compute_plan_hash(**fields))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "job",
        "owner",
        "epoch",
        "campaign",
        "coupon",
        "args_hash",
        "plan_campaign",
        "plan_coupon",
        "child",
        "scope",
        "plan_version",
    ],
)
async def test_token_business_ids_and_plan_are_bound(coupon, change):
    from oria.core.jobs import JobConflict

    c = coupon
    token, args, plan = c.token, c.args, c.plan
    if change in {"tenant", "job", "owner", "epoch"}:
        field = {"tenant": "tenant_id", "job": "job_id", "owner": "owner", "epoch": "epoch"}[change]
        token = token.model_copy(
            update={field: token.epoch + 1 if change == "epoch" else "foreign"}
        )
    elif change in {"campaign", "coupon", "args_hash"}:
        field = {
            "campaign": "campaign_id",
            "coupon": "coupon_batch_id",
            "args_hash": "coupon_spec_hash",
        }[change]
        args = args.model_copy(
            update={field: "sha256:" + "a" * 64 if change == "args_hash" else "foreign"}
        )
    elif change == "plan_campaign":
        plan = _changed_plan(plan, campaign_draft_id="foreign")
    elif change == "plan_coupon":
        plan = _changed_plan(plan, coupon_batch_draft_id="foreign")
    elif change in {"child", "scope"}:
        steps = [step.model_copy() for step in plan.child_steps]
        field = "canonical_args_hash" if change == "child" else "idempotency_scope"
        steps[0] = steps[0].model_copy(
            update={field: "sha256:" + "a" * 64 if change == "child" else "foreign"}
        )
        plan = _changed_plan(plan, child_steps=steps)
    elif change == "plan_version":
        plan = _changed_plan(plan, material_version="material-v2")
    before = await _counts(c.s)
    with pytest.raises((PermissionError, JobConflict, ValueError)):
        await c.service.reserve_coupon_materialization(token, args, plan, c.approval.approval_id)
    assert await _counts(c.s) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    ["campaigns", "campaign_rule_snapshot_refs", "coupon_batches", "recruitment_publications"],
)
async def test_current_draft_version_change_is_rejected(coupon, table):
    c = coupon
    async with c.s.db.business_sessions.begin() as session:
        await session.execute(text(f"UPDATE {table} SET version = version + 1"))
    before = await _counts(c.s)
    with pytest.raises((PermissionError, ValueError)):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["checkpoint", "status"])
async def test_saga_is_the_trusted_checkpoint_source(coupon, change):
    c = coupon
    async with c.s.db.business_sessions.begin() as session:
        assignment = (
            "checkpoint = 'other-saga-point'" if change == "checkpoint" else "status = 'failed'"
        )
        await session.execute(text(f"UPDATE launch_saga_states SET {assignment}"))
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == (1,) * 8


def _change_authority(c, monkeypatch, change):
    from oria.core.types import PolicyDecision

    s = c.s
    if change == "identity":
        s.path.write_text('{"bindings": []}')
    elif change in {"actor_policy", "worker_policy"}:
        field = "_trusted_actors" if change == "actor_policy" else "_trusted_executors"
        monkeypatch.setattr(s.runtime.policy, field, frozenset())
    elif change == "lease":
        s.clock.advance(30)
    elif change == "deadline":
        s.clock.advance(86400)
    elif change == "approval_expiry":
        s.clock.advance(2)
    else:
        original = s.runtime.policy.evaluate_now

        def changed(request, ctx):
            decision = original(request, ctx)
            if (
                request.action == "campaign:launch:request"
                and request.resource.resource_id == c.args.campaign_id
            ):
                if change == "version":
                    return decision.model_copy(update={"policy_version": "new-policy-version"})
                return PolicyDecision(
                    allow=False,
                    reason="synthetic resource revocation",
                    policy_version=decision.policy_version,
                )
            return decision

        monkeypatch.setattr(s.runtime.policy, "evaluate_now", changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["await", "commit"])
@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "actor_policy",
        "worker_policy",
        "resource_policy",
        "version",
        "lease",
        "deadline",
        "approval_expiry",
    ],
)
async def test_authority_changes_roll_back_at_await_and_commit(
    coupon, monkeypatch, boundary, change
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from oria.core.jobs import JobConflict

    c = coupon
    if change == "approval_expiry":
        c.approval = c.approval.model_copy(
            update={
                "approval_id": c.approval.approval_id + ":short-lived",
                "expires_at": c.s.clock.now() + timedelta(seconds=2),
            }
        )
        await c.approvals._repository.add(c.approval)
    before = await _counts(c.s)
    reached = []

    def mutate(session):
        if session.bind.url.database == str(c.s.config.data_paths.business_db):
            reached.append(True)
            _change_authority(c, monkeypatch, change)

    if boundary == "await":
        original = c.service._coupon_writer._tools._reserve_in_session

        async def change_after_insert(*args):
            result = await original(*args)
            reached.append(True)
            _change_authority(c, monkeypatch, change)
            return result

        monkeypatch.setattr(
            c.service._coupon_writer._tools, "_reserve_in_session", change_after_insert
        )
    else:
        event.listen(Session, "before_commit", mutate)
    try:
        with pytest.raises((PermissionError, JobConflict)):
            await c.service.reserve_coupon_materialization(
                c.token, c.args, c.plan, c.approval.approval_id
            )
    finally:
        if boundary == "commit":
            event.remove(Session, "before_commit", mutate)
    assert reached == [True]
    assert await _counts(c.s) == before
    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == c.claimed
    # Prove rollback and lock release by completing a fresh authorized reservation.
    monkeypatch.undo()
    c.s.path.write_text(c.s.directory.model_dump_json())
    if change in {"lease", "deadline", "approval_expiry"}:
        c.s.clock.value = c.claimed.heartbeat_at
    # Restoring the Policy fixture also needs the synthetic approver only for setup,
    # never for requester authorization during reservation.
    result = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, c.approval.approval_id
    )
    assert result.status == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "lease", "deadline", "identity", "resource_policy"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_invalid_current_authority_rejects_even_existing_history(
    coupon, monkeypatch, change, duplicate
):
    from tests.integration.test_v06_job_leases import _trusted_guard

    from oria.core.jobs import Job, JobConflict

    c = coupon
    if duplicate:
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    if change == "cancel":
        cancelled = Job.model_validate(
            c.claimed.model_dump()
            | {
                "status": "cancellation_requested",
                "cancel_requested": True,
                "revision": c.claimed.revision + 1,
            }
        )
        await c.s.repo.replace(c.claimed, cancelled, _trusted_guard)
    else:
        _change_authority(c, monkeypatch, change)
    before = await _counts(c.s)
    with pytest.raises((PermissionError, JobConflict)):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_failure_and_task_cancellation_release_both_locks(coupon, monkeypatch, cancel_task):
    import asyncio

    c = coupon
    original = c.service._coupon_writer._tools._reserve_in_session
    entered = asyncio.Event()

    async def fail_after_insert(*args):
        await original(*args)
        entered.set()
        if cancel_task:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic Business failure")

    monkeypatch.setattr(c.service._coupon_writer._tools, "_reserve_in_session", fail_after_insert)
    task = asyncio.create_task(
        c.service.reserve_coupon_materialization(c.token, c.args, c.plan, c.approval.approval_id)
    )
    await asyncio.wait_for(entered.wait(), 5)
    if cancel_task:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel_task else RuntimeError):
        await task
    assert await _counts(c.s) == (1,) * 8
    monkeypatch.setattr(c.service._coupon_writer._tools, "_reserve_in_session", original)
    assert (
        await asyncio.wait_for(
            c.service.reserve_coupon_materialization(
                c.token, c.args, c.plan, c.approval.approval_id
            ),
            5,
        )
    ).status == "reserved"
    await c.worker.heartbeat(c.token)


@pytest.mark.asyncio
@pytest.mark.parametrize("contender", ["takeover", "cancel", "revoke"])
async def test_two_sqlite_resources_reach_actual_update_and_cannot_cross_fence(
    coupon, monkeypatch, contender
):
    import asyncio

    from sqlalchemy import event

    from oria.api.jobs import JobAPI
    from oria.core.jobs import JobConflict
    from oria.orchestrator.job_leases import JobLeaseService
    from oria.storage.database import DatabaseResources
    from oria.storage.jobs import JobRepository

    c = coupon
    s = c.s
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_reserve = c.service._coupon_writer.reserve

    async def paused(*args):
        result = await original_reserve(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(c.service._coupon_writer, "reserve", paused)
    async with DatabaseResources(s.config) as other:
        repo = JobRepository(other.platform_sessions)
        second = JobLeaseService(
            s.runtime,
            repo,
            s.source,
            executor=s.worker,
            instance_id="independent-resource",
            clock=s.clock.now,
        )
        # Complete REAL authorization before holding the lock. Keep its REAL current
        # identity/Policy guard so the contender reaches CAS, not an audit INSERT.
        if contender == "takeover":
            original_authorize = second._authorize
            authorization = await original_authorize(c.claimed)

            async def preauthorized(job, **kwargs):
                assert job == c.claimed
                return authorization

            monkeypatch.setattr(second, "_authorize", preauthorized)
        elif contender == "cancel":
            api = JobAPI(s.runtime, repo, s.source, clock=s.clock.now)
            ctx = api._context(s.admin, "coupon-cancel-race", c.claimed)
            guard = await api._authorize(
                s.admin, ctx, (api._request("job:cancel", ctx, c.claimed),)
            )

            async def preauthorized(*args):
                return guard

            monkeypatch.setattr(api, "_authorize", preauthorized)
        else:
            approvals = ApprovalService(
                SQLiteApprovalRepository(other.platform_sessions),
                s.runtime.policy,
                clock=s.clock.now,
            )

        observed = []

        def observe(connection, cursor, statement, parameters, context, executemany):
            prefix = "UPDATE approvals SET" if contender == "revoke" else "UPDATE jobs SET"
            if statement.startswith(prefix):
                observed.append(statement.split(" WHERE")[0])
                attempted.set()

        event.listen(other._platform_engine.sync_engine, "before_cursor_execute", observe)
        first_task = asyncio.create_task(
            c.service.reserve_coupon_materialization(
                c.token, c.args, c.plan, c.approval.approval_id
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        if contender == "takeover":
            s.clock.advance(30)
            task = asyncio.create_task(second.claim(c.job.job_id))
        elif contender == "cancel":
            task = asyncio.create_task(api.cancel(c.job.job_id, s.admin, "coupon-cancel-race"))
        else:
            task = asyncio.create_task(approvals.invalidate_binding(c.approval, ctx=c.ctx))
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.1)
            assert await _counts(s) == (1,) * 8
            release.set()
            if contender == "takeover":
                with pytest.raises(JobConflict):
                    await first_task
                new = await asyncio.wait_for(task, 5)
                assert new.lease_epoch == c.token.epoch + 1
                assert await _counts(s) == (1,) * 8
                monkeypatch.setattr(second, "_authorize", original_authorize)
                assert (
                    await JobExecutionService(second, other).reserve_coupon_materialization(
                        lease_token(new), c.args, c.plan, c.approval.approval_id
                    )
                ).status == "reserved"
            else:
                assert (await first_task).status == "reserved"
                changed = await asyncio.wait_for(task, 5)
                assert changed.status == (
                    "invalidated" if contender == "revoke" else "cancellation_requested"
                )
            assert observed
            assert await _counts(s) == (1, 1, 1, 1, 2, 1, 1, 1)
            with pytest.raises((PermissionError, JobConflict)):
                await c.service.reserve_coupon_materialization(
                    c.token, c.args, c.plan, c.approval.approval_id
                )
        finally:
            release.set()
            for pending in (first_task, task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(first_task, task, return_exceptions=True)
            event.remove(other._platform_engine.sync_engine, "before_cursor_execute", observe)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "approval"])
async def test_change_after_authorization_before_platform_lock_is_not_lost(
    coupon, monkeypatch, change
):
    from contextlib import asynccontextmanager

    from oria.api.jobs import JobAPI
    from oria.core.jobs import JobConflict

    c = coupon
    original = c.s.repo._hold_execution_lease

    @asynccontextmanager
    async def change_before_lock(*args):
        if change == "cancel":
            await JobAPI(c.s.runtime, c.s.repo, c.s.source, clock=c.s.clock.now).cancel(
                c.job.job_id, c.s.admin, "before-coupon-fence"
            )
        else:
            await c.approvals.invalidate_binding(c.approval, ctx=c.ctx)
        async with original(*args) as session:
            yield session

    monkeypatch.setattr(c.s.repo, "_hold_execution_lease", change_before_lock)
    with pytest.raises((PermissionError, JobConflict)):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == (1,) * 8


@pytest.mark.asyncio
async def test_requester_cannot_prepare_a_self_approved_fixture(coupon):
    c = coupon
    approval = await c.approvals.create(
        approval_action="launch_approval",
        tool_name=LAUNCH_PLAN_TOOL_NAME,
        canonical_args_hash=c.plan.plan_hash,
        checkpoint_id=c.saga.checkpoint,
        expires_at=c.s.clock.now() + timedelta(hours=1),
        ctx=c.ctx,
    )
    with pytest.raises(PermissionError, match="requester cannot decide"):
        await c.approvals.decide(
            tenant_id=c.job.tenant_id,
            approval_id=approval.approval_id,
            decision="approve",
            reason=None,
            ctx=c.ctx,
        )
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, approval.approval_id
        )
    assert await _counts(c.s) == (1,) * 8


@pytest.mark.asyncio
async def test_reservation_reuses_existing_canonical_ledger_without_parallel_accounting(coupon):
    from oria.core.execution_ledger import ExecutionLedger

    c = coupon
    ledger = ExecutionLedger(c.s.db.business_sessions, clock=c.s.clock.now)
    existing = await ledger.reserve_for_args(
        execution_id="existing-domain-reservation",
        tenant_id=c.job.tenant_id,
        tool_name="materialize_coupon_batch",
        tool_schema_version=1,
        schema=MaterializeCouponBatchArgs,
        args=c.args.model_dump(),
        stable_business_id=f"{c.args.campaign_id}:{c.args.coupon_spec_hash}",
        checkpoint_id=c.saga.checkpoint,
    )
    assert (
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
        == existing
    )
    await ledger.mark_executing(existing)
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(
            c.token, c.args, c.plan, c.approval.approval_id
        )
    assert await _counts(c.s) == (1, 1, 1, 1, 2, 1, 1, 1)


@pytest.mark.asyncio
async def test_reservation_does_not_use_or_advance_real_accepted_pointer(coupon):
    from tests.integration.test_v06_job_leases import _save

    c = coupon
    await _save(c.s, c.token, "actual-saver-checkpoint")
    published = await c.worker.publish_checkpoint(c.token, "actual-saver-checkpoint")
    result = await c.service.reserve_coupon_materialization(
        c.token, c.args, c.plan, c.approval.approval_id
    )
    assert result.checkpoint_id == c.saga.checkpoint != published.accepted_checkpoint_id
    assert await c.s.repo.get(c.job.tenant_id, c.job.job_id) == published


@pytest.mark.asyncio
async def test_job_without_persisted_draft_cannot_reserve(coupon):
    c = coupon
    job = await c.s.add(scenario="campaign", actor=c.s.admin)
    token = lease_token(await c.worker.claim(job.job_id))
    suffix = job.job_id.removeprefix("job_")
    args = c.args.model_copy(
        update={"campaign_id": f"campaign_{suffix}", "coupon_batch_id": f"coupon_{suffix}"}
    )
    with pytest.raises(PermissionError):
        await c.service.reserve_coupon_materialization(token, args, c.plan, c.approval.approval_id)
    assert await _counts(c.s) == (1,) * 8


@pytest.mark.asyncio
async def test_other_business_writer_cannot_change_bound_facts_before_commit(coupon, monkeypatch):
    import asyncio

    from sqlalchemy import event

    from oria.storage.database import DatabaseResources

    c = coupon
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = c.service._coupon_writer.prepare

    async def pause(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(c.service._coupon_writer, "prepare", pause)
    async with DatabaseResources(c.s.config) as other:

        async def change_version():
            async with other.business_sessions.begin() as session:
                await session.execute(
                    text("UPDATE recruitment_publications SET version = version + 1")
                )

        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE recruitment_publications SET"):
                attempted.set()

        event.listen(other._business_engine.sync_engine, "before_cursor_execute", observe)
        first = asyncio.create_task(
            c.service.reserve_coupon_materialization(
                c.token, c.args, c.plan, c.approval.approval_id
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        competing = asyncio.create_task(change_version())
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(competing), 0.1)
            release.set()
            assert (await first).status == "reserved"
            await asyncio.wait_for(competing, 5)
            with pytest.raises(PermissionError):
                await c.service.reserve_coupon_materialization(
                    c.token, c.args, c.plan, c.approval.approval_id
                )
            assert await _counts(c.s) == (1, 1, 1, 1, 2, 1, 1, 1)
        finally:
            release.set()
            for task in (first, competing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, competing, return_exceptions=True)
            event.remove(other._business_engine.sync_engine, "before_cursor_execute", observe)
