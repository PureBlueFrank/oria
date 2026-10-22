"""Explicit Community-only coupon dispatch; no network or Graph assembly."""

import asyncio

from sqlalchemy import event
from sqlalchemy.orm import Session

from oria.adapters.launch import InMemoryCouponBatchAdapter
from oria.core.approvals import ApprovalResumeRequest, approval_resume_rejection
from oria.core.job_leases import JobLeaseToken, validate_lease
from oria.core.jobs import CampaignJobSpec
from oria.core.types import AuthorizationContext, AuthorizationRequest, ResourceRef
from oria.domain.launch import LAUNCH_PLAN_POLICY, LAUNCH_PLAN_TOOL_NAME, MaterializeCouponBatchArgs
from oria.domain.ledger import LaunchPlan, Receipt, ToolExecution
from oria.orchestrator.job_execution import JobExecutionService
from oria.orchestrator.job_leases import JobLeaseService, _LeaseAuthorization
from oria.storage.database import DatabaseResources
from oria.storage.job_coupon_dispatch import JobCouponDispatchWriter


class JobCouponDispatchService:
    def __init__(
        self,
        leases: JobLeaseService,
        databases: DatabaseResources,
        adapter: InMemoryCouponBatchAdapter,
        *,
        timeout_seconds: float = 5,
    ) -> None:
        if type(adapter) is not InMemoryCouponBatchAdapter:
            raise ValueError("Job coupon dispatch currently requires the in-memory adapter")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 5
        ):
            raise ValueError("Mock dispatch timeout must be in (0, 5] seconds")
        self._execution = JobExecutionService(leases, databases)
        self._leases, self._databases = leases, databases
        self._adapter, self._timeout = adapter, timeout_seconds
        self._writer = JobCouponDispatchWriter(databases.business_sessions)

    async def materialize(
        self,
        token: JobLeaseToken,
        args: MaterializeCouponBatchArgs,
        plan: LaunchPlan,
        approval_id: str,
    ) -> ToolExecution:
        args = MaterializeCouponBatchArgs.model_validate(args.model_dump())
        plan = LaunchPlan.model_validate(plan.model_dump())
        job, ctx, guard = await self._leases._leased(token, business_resource_id=args.campaign_id)
        suffix = job.job_id.removeprefix("job_")
        if not isinstance(job.submission.spec, CampaignJobSpec) or (
            args.campaign_id,
            args.coupon_batch_id,
        ) != (f"campaign_{suffix}", f"coupon_{suffix}"):
            raise PermissionError("coupon dispatch must belong to the campaign Job")
        request = AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=LAUNCH_PLAN_POLICY.required_action,
            resource=ResourceRef(
                tenant_id=job.tenant_id, resource_type="campaign", resource_id=args.campaign_id
            ),
            context=AuthorizationContext(correlation_id=ctx.correlation_id),
        )
        launch_guard = _LeaseAuthorization(self._leases, job, ctx, (request,))
        launch_guard._check(request, await self._leases._policy.authorize(request, ctx))

        async def execute() -> ToolExecution:
            async with self._leases._repository._hold_execution_lease(
                job, token, self._leases._now
            ) as platform:
                approval = await self._execution._approvals.bind(platform).get(
                    job.tenant_id, approval_id
                )
                if approval is None:
                    raise PermissionError("launch approval is unavailable")
                resume = ApprovalResumeRequest(
                    approval_id=approval_id,
                    approval_action="launch_approval",
                    tool_name=LAUNCH_PLAN_TOOL_NAME,
                    canonical_args_hash=plan.plan_hash,
                    checkpoint_id=self._writer._facts.launch_saga(job, args).checkpoint,
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
                # The durable executing state must outlive any later cancellation,
                # process loss or outcome rollback. Never call before this commit.
                async with self._databases.business_sessions.begin() as session:
                    event.listen(session.sync_session, "before_commit", check)
                    try:
                        execution = await self._writer.load(session, job, args, plan)
                        await guard()
                        await launch_guard()
                        check()
                        if execution.status == "executing":
                            # This fence is now exclusively held: an executing row
                            # from an earlier call is an orphan, never a retry grant.
                            result = await self._writer.finish(
                                session,
                                execution,
                                args,
                                None,
                                ctx=ctx,
                                epoch=token.epoch,
                                policy_version=approval.policy_version,
                                now=self._leases._now(),
                            )
                        elif execution.status == "reserved":
                            result = await self._writer.admit(
                                session,
                                execution,
                                ctx=ctx,
                                epoch=token.epoch,
                                policy_version=approval.policy_version,
                                now=self._leases._now(),
                            )
                        else:
                            result = execution
                        await guard()
                        await launch_guard()
                        check()
                        await session.commit()
                    finally:
                        event.remove(session.sync_session, "before_commit", check)
                if execution.status != "reserved":
                    return result

                async with self._databases.business_sessions.begin() as session:
                    event.listen(session.sync_session, "before_commit", check)
                    try:
                        executing = await self._writer.load(session, job, args, plan)
                        if executing != result:
                            raise PermissionError("coupon dispatch admission changed")
                        await guard()
                        await launch_guard()
                        check()
                        receipt: Receipt | None = None
                        try:
                            assert job.lease_expires_at is not None  # validated by lease guard
                            timeout = min(
                                self._timeout,
                                (job.lease_expires_at - self._leases._now()).total_seconds(),
                                (approval.expires_at - self._leases._now()).total_seconds(),
                            )
                            async with asyncio.timeout(timeout):
                                check()
                                received = await self._adapter.materialize(
                                    args, idempotency_key=executing.idempotency_key
                                )
                            receipt = Receipt.model_validate(received.model_dump())
                            if (
                                receipt.adapter_id != self._adapter.adapter_id
                                or receipt.resource_ref != f"coupon_batch:{args.coupon_batch_id}"
                                or receipt.received_at < executing.updated_at
                                or receipt.received_at > self._leases._now()
                            ):
                                receipt = None
                        except Exception:
                            # An adapter error does not prove the remote side did
                            # nothing. Do not persist exception text or blind-retry.
                            receipt = None
                        check()
                        result = await self._writer.finish(
                            session,
                            executing,
                            args,
                            receipt,
                            ctx=ctx,
                            epoch=token.epoch,
                            policy_version=approval.policy_version,
                            now=self._leases._now(),
                        )
                        await guard()
                        await launch_guard()
                        check()
                        await session.commit()
                    finally:
                        event.remove(session.sync_session, "before_commit", check)
                return result

        return await self._leases._write(execute())
