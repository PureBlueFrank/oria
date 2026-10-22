"""Pure Job-to-Graph input adapters; these values do not authorize execution."""

from __future__ import annotations

import json

from oria.agent.attribution import initial_attribution_state
from oria.agent.state import ResearchState
from oria.core.context import Context
from oria.core.jobs import AttributionJobSpec, CampaignJobSpec, Job, JobConflict
from oria.orchestrator.scenario_a import ScenarioAWorkflowRequest, initial_scenario_a_state
from oria.orchestrator.state import WorkflowState


def _initial_job(job: Job, ctx: Context) -> Job:
    # Revalidate model_copy/unsafe construction before consuming a durable snapshot.
    job = Job.model_validate(job.model_dump())
    if (
        job.tenant_id,
        job.session_id,
        job.thread_id,
        job.run_id,
        job.job_id,
        job.requester_subject_id,
    ) != (
        ctx.tenant_id,
        ctx.session_id,
        ctx.thread_id,
        ctx.run_id,
        ctx.job_id,
        ctx.actor.subject_id,
    ) or (ctx.executor.tenant_id != job.tenant_id or ctx.executor.kind != "service"):
        raise PermissionError("job execution context does not match its owner and identifiers")
    if (
        job.status != "queued"
        or job.attempt
        or job.lease_epoch
        or job.cancel_requested
        or job.accepted_checkpoint_id is not None
        or ctx.checkpoint_id is not None
    ):
        raise JobConflict("only an unexecuted job can produce initial graph input")
    return job


def _confirmed_slots(spec: CampaignJobSpec | AttributionJobSpec) -> str:
    return json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def campaign_job_input(
    job: Job,
    ctx: Context,
    *,
    template: ScenarioAWorkflowRequest,
) -> WorkflowState:
    """Bind confirmed slots to an explicit trusted business-policy template.

    No CLI fixture defaults, synthetic principals, database writes or Graph calls.
    Policy/material/notification/placement settings remain server-owned. Template
    expiry is explicit and must fit the Job deadline; no silent clamping occurs.
    The caller must claim/fence and reauthorize before executing this initial state.
    """
    job = _initial_job(job, ctx)
    if not isinstance(job.submission.spec, CampaignJobSpec):
        raise ValueError("campaign input requires a campaign job")
    template = ScenarioAWorkflowRequest.model_validate(template.model_dump())
    if max(template.approval_expires_at, template.external_wait_expires_at) > job.timeout_at:
        raise ValueError("workflow policy expiry exceeds the job deadline")
    spec = job.submission.spec
    suffix = job.job_id.removeprefix("job_")
    request = ScenarioAWorkflowRequest.model_validate(
        template.model_dump()
        | {
            "user_request": "Prepare a campaign using these confirmed slots: "
            + _confirmed_slots(spec),
            "effective_at": job.created_at,
            "max_candidates": spec.max_candidates,
            "enrollment_mode": spec.enrollment_mode,
            "draft": template.draft.model_dump()
            | {
                "campaign_id": f"campaign_{suffix}",
                "coupon_batch_id": f"coupon_{suffix}",
                "recruitment_publication_id": f"recruitment_{suffix}",
            },
            "circle_run_id": f"{job.job_id}:circle:v1",
            "coupon_link_idempotency_key": f"{job.job_id}:coupon-link:v1",
            "assortment_idempotency_key": f"{job.job_id}:assortment:v1",
            "placement_idempotency_key": f"{job.job_id}:placement:v1",
            "notification_idempotency_prefix": f"{job.job_id}:notification:v1",
        }
    )
    return initial_scenario_a_state(
        request,
        meta={
            "tenant_id": ctx.tenant_id,
            "session_id": ctx.session_id,
            "thread_id": ctx.thread_id,
            "run_id": ctx.run_id,
            "job_id": ctx.job_id,
            "requester_subject_id": ctx.actor.subject_id,
        },
    )


def attribution_job_input(job: Job, ctx: Context) -> ResearchState:
    """Use the permanent Scenario B ResearchSpec and its fixed prompt version."""
    job = _initial_job(job, ctx)
    spec = job.submission.spec
    if not isinstance(spec, AttributionJobSpec):
        raise ValueError("attribution input requires an attribution job")
    return initial_attribution_state(
        question="Investigate the metric and scope in these confirmed slots: "
        + _confirmed_slots(spec),
        analysis_period=f"{spec.period.start_date.isoformat()}/{spec.period.end_date.isoformat()}",
        tenant_id=ctx.tenant_id,
    )
