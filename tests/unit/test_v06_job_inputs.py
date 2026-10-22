"""Owner bindings and explicit business policy at the Job-to-Graph seam."""

from dataclasses import replace
from datetime import timedelta

import pytest

from oria.core.context import Context
from oria.core.jobs import Job, JobConflict, JobSubmission, submission_hash
from oria.orchestrator.job_inputs import attribution_job_input, campaign_job_input
from oria.orchestrator.local_executor import FIXTURE_NOW, campaign_admin, default_request
from oria.orchestrator.scenario_a import ScenarioAWorkflowRequest
from oria.permission.local import local_cli_executor

pytestmark = pytest.mark.unit


def context(runtime=None):
    return Context(
        runtime=runtime,
        actor=campaign_admin(),
        executor=local_cli_executor(),
        session_id="session_" + "1" * 32,
        thread_id="thread_" + "2" * 32,
        run_id="run_" + "3" * 32,
        correlation_id="job-input-test",
        job_id="job_" + "4" * 32,
    )


def job(ctx, scenario="campaign", *, max_candidates=7):
    spec = (
        {
            "scenario": "campaign",
            "categories": ["餐饮"],
            "cities": ["上海"],
            "enrollment_mode": "auto",
            "max_candidates": max_candidates,
        }
        if scenario == "campaign"
        else {
            "scenario": "attribution",
            "metric": "redemption_rate",
            "region": "east",
            "category": "full_service",
            "period": {"start_date": "2026-08-30", "end_date": "2026-08-31"},
        }
    )
    submission = JobSubmission.model_validate({"session_id": ctx.session_id, "spec": spec})
    return Job(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        session_id=ctx.session_id,
        thread_id=ctx.thread_id,
        run_id=ctx.run_id,
        requester_subject_id=ctx.actor.subject_id,
        requester_issuer="synthetic-issuer",
        requester_external_subject="synthetic-user",
        submission=submission,
        request_hash=submission_hash(submission),
        idempotency_key="a" * 64,
        created_at=FIXTURE_NOW,
        updated_at=FIXTURE_NOW,
        timeout_at=FIXTURE_NOW + timedelta(days=1),
    )


def template():
    request = default_request(campaign_id="old-fixture-id", user_request="raw-input-sentinel")
    return ScenarioAWorkflowRequest.model_validate(
        request.model_dump()
        | {
            "approval_expires_at": FIXTURE_NOW + timedelta(hours=1),
            "external_wait_expires_at": FIXTURE_NOW + timedelta(hours=2),
        }
    )


def test_campaign_slots_bind_ids_and_preserve_server_policy_without_effects():
    ctx = context()
    value = job(ctx)
    trusted = template()
    state = campaign_job_input(value, ctx, template=trusted)
    request = ScenarioAWorkflowRequest.model_validate(state["plan"]["steps"][0]["params"])
    assert request.max_candidates == 7 and request.enrollment_mode == "auto"
    assert request.effective_at == value.created_at
    assert request.draft.campaign_id == "campaign_" + "4" * 32
    assert request.draft.coupon_batch_id != trusted.draft.coupon_batch_id
    assert request.draft.material_version == trusted.draft.material_version
    assert request.placement_spec == trusted.placement_spec
    assert request.notification_channel == trusted.notification_channel
    assert request.assortment_policy_ref == trusted.assortment_policy_ref
    assert request.placement_idempotency_key == value.job_id + ":placement:v1"
    assert "raw-input-sentinel" not in str(state)
    assert "餐饮" in request.user_request and "上海" in request.user_request
    assert state["meta"]["job_id"] == value.job_id
    assert state["meta"]["requester_subject_id"] == ctx.actor.subject_id
    assert state == campaign_job_input(value, ctx, template=trusted)
    # All three inputs remain unchanged; template is reusable for another Job.
    assert trusted.draft.campaign_id == "old-fixture-id" and value.status == "queued"


def test_attribution_uses_existing_prompt_and_explicit_scope():
    ctx = context()
    value = job(ctx, "attribution")
    state = attribution_job_input(value, ctx)
    assert state["effective_at"] == "2026-08-30/2026-08-31"
    assert "redemption_rate" in state["user_request"] and "east" in state["user_request"]
    assert ctx.tenant_id in state["messages"][1]["content"]
    assert state["model_turns"] == 0 and state["tool_calls_total"] == 0
    assert "synthetic-issuer" not in str(state)


@pytest.mark.parametrize(
    "field", ["session_id", "thread_id", "run_id", "job_id", "actor", "tenant", "executor"]
)
def test_wrong_context_is_rejected_before_constructing_state(field):
    ctx = context()
    value = job(ctx)
    if field == "actor":
        changed = replace(ctx, actor=ctx.actor.model_copy(update={"subject_id": "peer"}))
    elif field == "executor":
        changed = replace(ctx, executor=ctx.actor)
    elif field == "tenant":
        changed = replace(
            ctx,
            actor=ctx.actor.model_copy(update={"tenant_id": "foreign"}),
            executor=ctx.executor.model_copy(update={"tenant_id": "foreign"}),
        )
    else:
        changed = replace(ctx, **{field: "wrong-id"})
    with pytest.raises(PermissionError):
        campaign_job_input(value, changed, template=template())


@pytest.mark.parametrize(
    "change",
    [
        {"status": "cancelled"},
        {"status": "succeeded"},
        {"status": "failed"},
        {"attempt": 1, "lease_epoch": 1},
        {"cancel_requested": True},
        {"accepted_checkpoint_ns": "epoch-1", "accepted_checkpoint_id": "checkpoint"},
    ],
)
def test_existing_or_cancelled_execution_cannot_be_reinitialized(change):
    ctx = context()
    value = Job.model_validate(job(ctx, "attribution").model_dump() | change)
    with pytest.raises(JobConflict):
        attribution_job_input(value, ctx)


@pytest.mark.parametrize("field", ["approval_expires_at", "external_wait_expires_at"])
def test_policy_deadlines_cannot_exceed_job_timeout(field):
    ctx = context()
    value = job(ctx)
    trusted = ScenarioAWorkflowRequest.model_validate(
        template().model_dump() | {field: value.timeout_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="deadline"):
        campaign_job_input(value, ctx, template=trusted)


def test_wrong_scenario_and_corrupted_snapshots_fail_closed():
    ctx = context()
    with pytest.raises(ValueError, match="attribution job"):
        attribution_job_input(job(ctx), ctx)
    with pytest.raises(ValueError, match="campaign job"):
        campaign_job_input(job(ctx, "attribution"), ctx, template=template())
    unsafe = job(ctx, "attribution").model_copy(update={"request_hash": "b" * 64})
    with pytest.raises(ValueError, match="snapshot mismatch"):
        attribution_job_input(unsafe, ctx)


def test_resume_context_cannot_generate_fresh_input():
    ctx = context()
    value = job(ctx, "attribution")
    with pytest.raises(JobConflict):
        attribution_job_input(value, replace(ctx, checkpoint_id="existing-checkpoint"))
