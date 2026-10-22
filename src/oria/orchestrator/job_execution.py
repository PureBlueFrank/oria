"""Explicit trusted worker boundaries for local draft and approved coupon reservation."""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from oria.core.approvals import (
    Approval,
    ApprovalResumeRequest,
    approval_resume_rejection,
    canonical_args_hash,
)
from oria.core.execution_ledger import ExecutionLedger
from oria.core.job_leases import JobLeaseToken, validate_lease
from oria.core.jobs import CampaignJobSpec, JobConflict
from oria.core.types import AuthorizationContext, AuthorizationRequest, EventEnvelope, ResourceRef
from oria.domain.launch import (
    LAUNCH_PLAN_POLICY,
    LAUNCH_PLAN_TOOL_NAME,
    MATERIALIZE_TOOL_NAME,
    CampaignDraft,
    CampaignDraftSpec,
    LaunchApprovalBinding,
    MaterializeCouponBatchArgs,
    build_campaign_draft,
)
from oria.domain.ledger import LaunchPlan, ToolExecution
from oria.orchestrator.job_leases import JobLeaseService, _LeaseAuthorization
from oria.rag.models import CampaignRuleSnapshot
from oria.storage.database import DatabaseResources
from oria.storage.job_execution import JobCampaignDraftWriter, JobCouponReservationWriter
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import BusinessRepositoryError


class JobExecutionService:
    """Opt in explicitly; does not execute Graph or authorize any external call."""

    def __init__(self, leases: JobLeaseService, databases: DatabaseResources) -> None:
        if databases.platform_backend != "sqlite" or databases.business_backend != "sqlite":
            raise ValueError("local Job execution fencing currently requires SQLite")
        if leases._repository._sessions is not databases.platform_sessions:
            raise ValueError("Job repository must use the supplied platform sessions")
        platform = databases.platform_sessions.kw["bind"].url.database
        business = databases.business_sessions.kw["bind"].url.database
        if not platform or not business or platform == business:
            raise ValueError("execution fencing requires separate file-backed databases")
        self._leases, self._databases = leases, databases
        self._writer = JobCampaignDraftWriter(databases.business_sessions)
        self._coupon_writer = JobCouponReservationWriter(databases.business_sessions)
        self._approvals = SQLiteApprovalRepository(databases.platform_sessions)

    async def persist_campaign_draft(
        self, token: JobLeaseToken, spec: CampaignDraftSpec, snapshot: CampaignRuleSnapshot
    ) -> CampaignDraft:
        spec = CampaignDraftSpec.model_validate(spec.model_dump())
        snapshot = CampaignRuleSnapshot.model_validate(
            snapshot.internal_payload()
            | {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
            }
        )
        job, ctx, guard = await self._leases._leased(token, business_resource_id=spec.campaign_id)
        if job.status != "running" or job.cancel_requested:
            raise JobConflict("cancelled execution cannot persist a draft")
        suffix = job.job_id.removeprefix("job_")
        if not isinstance(job.submission.spec, CampaignJobSpec) or (
            spec.campaign_id,
            spec.coupon_batch_id,
            spec.recruitment_publication_id,
        ) != (f"campaign_{suffix}", f"coupon_{suffix}", f"recruitment_{suffix}"):
            raise PermissionError("draft must belong to the campaign Job")
        if snapshot.enrollment_policy.mode != job.submission.spec.enrollment_mode:
            raise ValueError("draft enrollment mode does not match the Job")
        draft = build_campaign_draft(
            spec,
            snapshot,
            tenant_id=ctx.tenant_id,
            now=job.created_at,
            rule_ref_id=f"rule_ref_{suffix}",
        )
        args = json.dumps(
            {
                "spec": spec.model_dump(mode="json"),
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        now = self._leases._now()
        reservation = ToolExecution(
            execution_id=f"{job.job_id}:campaign-draft:v1",
            tenant_id=job.tenant_id,
            tool_name="persist_campaign_draft",
            idempotency_key=f"{job.job_id}:campaign-draft:v1",
            canonical_args_hash=f"sha256:{hashlib.sha256(args.encode()).hexdigest()}",
            checkpoint_id=f"{token.checkpoint_ns}:campaign-draft",
            created_at=now,
            updated_at=now,
        )

        async def persist() -> CampaignDraft:
            async with self._leases._repository._hold_execution_lease(
                job, token, self._leases._now
            ):
                await guard()
                validate_lease(job, token, self._leases._now())
                try:
                    async with self._databases.business_sessions() as session:

                        def before_commit(_: Session) -> None:
                            guard.check_commit()
                            validate_lease(job, token, self._leases._now())

                        event.listen(session.sync_session, "before_commit", before_commit)
                        try:
                            async with session.begin():
                                await self._writer.persist(session, draft, reservation, ctx)
                                await guard()
                                validate_lease(job, token, self._leases._now())
                        finally:
                            event.remove(session.sync_session, "before_commit", before_commit)
                except SQLAlchemyError:
                    raise BusinessRepositoryError("Job draft persistence failed") from None
            return draft

        return await self._leases._write(persist())

    async def request_launch_approval(
        self,
        token: JobLeaseToken,
        args: MaterializeCouponBatchArgs,
        plan: LaunchPlan,
    ) -> LaunchApprovalBinding:
        """Prepare a fixed Job launch approval without dispatch or a Graph transition."""
        args = MaterializeCouponBatchArgs.model_validate(args.model_dump())
        plan = LaunchPlan.model_validate(plan.model_dump())
        job, ctx, guard = await self._leases._leased(token, business_resource_id=args.campaign_id)
        suffix = job.job_id.removeprefix("job_")
        if not isinstance(job.submission.spec, CampaignJobSpec) or (
            args.campaign_id,
            args.coupon_batch_id,
        ) != (f"campaign_{suffix}", f"coupon_{suffix}"):
            raise PermissionError("launch approval must belong to the campaign Job")
        expected_saga = self._coupon_writer.launch_saga(job, args)
        requests = tuple(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=action,
                resource=ResourceRef(
                    tenant_id=job.tenant_id, resource_type=kind, resource_id=resource_id
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            )
            for action, kind, resource_id in (
                (LAUNCH_PLAN_POLICY.required_action, "campaign", args.campaign_id),
                ("approval:launch:request", "approval", expected_saga.checkpoint),
            )
        )
        launch_guard = _LeaseAuthorization(self._leases, job, ctx, requests)
        for request in requests:
            decision = await self._leases._policy.authorize(request, ctx)
            launch_guard._check(request, decision)

        def check(_: Session | None = None) -> None:
            guard.check_commit()
            launch_guard.check_commit()
            validate_lease(job, token, self._leases._now())

        async def prepare() -> LaunchApprovalBinding:
            # A committed saga without an approval is deliberately inert. Replays
            # repair this gap rather than pretending the two DBs commit atomically.
            async with (
                self._leases._repository._hold_execution_lease(job, token, self._leases._now),
                self._databases.business_sessions.begin() as session,
            ):
                await guard()
                await launch_guard()
                event.listen(session.sync_session, "before_commit", check)
                try:
                    await self._coupon_writer.ensure_launch(session, job, args, plan)
                    await guard()
                    await launch_guard()
                    check()
                    await session.commit()
                finally:
                    event.remove(session.sync_session, "before_commit", check)

            # Reacquire both locks and revalidate after the durable Business commit.
            # The Business transaction below only holds a no-op write lock; it is
            # rolled back after the Platform approval/audit/outbox commit.
            async with (
                self._leases._repository._hold_execution_lease(
                    job, token, self._leases._now
                ) as platform,
                self._databases.business_sessions() as session,
            ):
                await guard()
                await launch_guard()
                checkpoint = await self._coupon_writer.require_launch(session, job, args, plan)
                repository = self._approvals.bind(platform)
                approval_id = f"{job.job_id}:launch-approval:v1"
                approval = await repository.get(job.tenant_id, approval_id)
                now = self._leases._now()
                policy_version = self._leases._policy.evaluate_now(requests[-1], ctx).policy_version
                if approval is None:
                    approval = Approval(
                        approval_id=approval_id,
                        tenant_id=job.tenant_id,
                        approval_action="launch_approval",
                        tool_name=LAUNCH_PLAN_TOOL_NAME,
                        canonical_args_hash=plan.plan_hash,
                        checkpoint_id=checkpoint,
                        policy_version=policy_version,
                        expires_at=min(now + timedelta(hours=1), job.timeout_at),
                        requester=job.requester_subject_id,
                        created_at=now,
                        updated_at=now,
                    )
                    is_new = True
                else:
                    is_new = False

                def check_approval(_: Session | None = None) -> None:
                    check()
                    current = self._leases._policy.evaluate_now(requests[-1], ctx)
                    if (
                        approval.approval_action != "launch_approval"
                        or approval.tool_name != LAUNCH_PLAN_TOOL_NAME
                        or approval.canonical_args_hash != plan.plan_hash
                        or approval.checkpoint_id != checkpoint
                        or approval.policy_version != current.policy_version
                        or approval.requester != job.requester_subject_id
                        or approval.business_binding is not None
                        or approval.decider == approval.requester
                        or approval.status not in {"pending", "approved"}
                        or approval.expires_at <= self._leases._now()
                        or approval.expires_at > job.timeout_at
                        or approval.expires_at > approval.created_at + timedelta(hours=1)
                        or approval.created_at < job.created_at
                        or approval.updated_at > self._leases._now()
                    ):
                        raise PermissionError("existing launch approval is not reusable")

                check_approval()
                event.listen(platform.sync_session, "before_commit", check_approval)
                try:
                    if is_new:
                        await repository.add(
                            approval,
                            EventEnvelope(
                                event_id=f"{approval_id}:created",
                                occurred_at=approval.created_at,
                                tenant_id=job.tenant_id,
                                actor=ctx.actor.subject_id,
                                action="approval.created",
                                resource=ResourceRef(
                                    tenant_id=job.tenant_id,
                                    resource_type="approval",
                                    resource_id=approval_id,
                                ),
                                decision="allow",
                                policy_version=approval.policy_version,
                                args_hash=plan.plan_hash,
                                result="success",
                                correlation_id=ctx.correlation_id,
                                payload={"approval_action": "launch_approval", "status": "pending"},
                            ),
                        )
                    await guard()
                    await launch_guard()
                    check_approval()
                    await platform.commit()
                finally:
                    event.remove(platform.sync_session, "before_commit", check_approval)
            return LaunchApprovalBinding(plan=plan, approval=approval)

        return await self._leases._write(prepare())

    async def reserve_coupon_materialization(
        self,
        token: JobLeaseToken,
        args: MaterializeCouponBatchArgs,
        plan: LaunchPlan,
        approval_id: str,
    ) -> ToolExecution:
        """Reserve one approved child; this never grants permission to dispatch."""
        args = MaterializeCouponBatchArgs.model_validate(args.model_dump())
        plan = LaunchPlan.model_validate(plan.model_dump())
        job, ctx, guard = await self._leases._leased(token, business_resource_id=args.campaign_id)
        suffix = job.job_id.removeprefix("job_")
        if not isinstance(job.submission.spec, CampaignJobSpec) or (
            args.campaign_id,
            args.coupon_batch_id,
        ) != (f"campaign_{suffix}", f"coupon_{suffix}"):
            raise PermissionError("coupon reservation must belong to the campaign Job")
        request = AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=LAUNCH_PLAN_POLICY.required_action,
            resource=ResourceRef(
                tenant_id=job.tenant_id,
                resource_type="campaign",
                resource_id=args.campaign_id,
            ),
            context=AuthorizationContext(correlation_id=ctx.correlation_id),
        )
        launch_guard = _LeaseAuthorization(self._leases, job, ctx, (request,))
        # Audited authorization happens BEFORE the platform lock. All checks inside
        # the locked transactions use the same read-only Policy evaluation path.
        decision = await self._leases._policy.authorize(request, ctx)
        launch_guard._check(request, decision)
        args_hash = canonical_args_hash(
            tool_name=MATERIALIZE_TOOL_NAME,
            tool_schema_version=1,
            schema=MaterializeCouponBatchArgs,
            args=args.model_dump(),
        )

        async def reserve() -> ToolExecution:
            async with self._leases._repository._hold_execution_lease(
                job, token, self._leases._now
            ) as platform:
                await guard()
                await launch_guard()
                async with self._databases.business_sessions.begin() as session:
                    checkpoint = await self._coupon_writer.prepare(session, job, args, plan)
                    approval = await self._approvals.bind(platform).get(job.tenant_id, approval_id)
                    if approval is None:
                        raise PermissionError("launch approval is unavailable")
                    resume = ApprovalResumeRequest(
                        approval_id=approval_id,
                        approval_action="launch_approval",
                        tool_name=LAUNCH_PLAN_TOOL_NAME,
                        canonical_args_hash=plan.plan_hash,
                        checkpoint_id=checkpoint,
                    )

                    def check(_: Session | None = None) -> None:
                        guard.check_commit()
                        launch_guard.check_commit()
                        validate_lease(job, token, self._leases._now())
                        current = self._leases._policy.evaluate_now(request, ctx)
                        rejection = approval_resume_rejection(
                            approval,
                            resume,
                            LAUNCH_PLAN_POLICY,
                            policy_version=current.policy_version,
                            now=self._leases._now(),
                        )
                        if rejection is not None or (
                            approval.requester != job.requester_subject_id
                            or approval.decider == approval.requester
                            or approval.business_binding is not None
                        ):
                            raise PermissionError("approved launch binding is not valid")

                    check()
                    now = self._leases._now()
                    reservation = ToolExecution(
                        execution_id=f"{job.job_id}:coupon-materialization:v1",
                        tenant_id=job.tenant_id,
                        tool_name=MATERIALIZE_TOOL_NAME,
                        idempotency_key=ExecutionLedger.build_idempotency_key(
                            f"{args.campaign_id}:{args.coupon_spec_hash}",
                            args_hash,
                        ),
                        canonical_args_hash=args_hash,
                        checkpoint_id=checkpoint,
                        created_at=now,
                        updated_at=now,
                    )
                    event.listen(session.sync_session, "before_commit", check)
                    try:
                        result = await self._coupon_writer.reserve(session, reservation)
                        await guard()
                        await launch_guard()
                        check()
                        # Commit while the hook is still installed.
                        await session.commit()
                    finally:
                        event.remove(session.sync_session, "before_commit", check)
            return result

        return await self._leases._write(reserve())
