"""Local reference acceptance with real approval/facts, still no business effect.

Only test code imports this module. A durable synthetic history row does not update
Oria's execution ledger/coupon/saga, send a network request, or grant redispatch.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel
from tests.integration.job_authority_reference import _JobBoundReceiver

from oria.core.approvals import ApprovalResumeRequest, approval_resume_rejection
from oria.core.dispatch import (
    DispatchObservation,
    DispatchOperation,
    DispatchRejected,
    DispatchRequest,
    validate_dispatch_payload,
)
from oria.core.job_leases import JobLeaseToken
from oria.core.jobs import CampaignJobSpec
from oria.core.types import AuthorizationContext, AuthorizationRequest, ResourceRef
from oria.domain.launch import (
    LAUNCH_PLAN_POLICY,
    LAUNCH_PLAN_TOOL_NAME,
    MATERIALIZE_TOOL_NAME,
    MaterializeCouponBatchArgs,
)
from oria.domain.ledger import LaunchPlan
from oria.orchestrator.job_execution import JobExecutionService
from oria.orchestrator.job_leases import JobLeaseService, _LeaseAuthorization
from oria.storage.database import DatabaseResources
from oria.storage.job_coupon_dispatch import JobCouponDispatchWriter


class _ApprovedCouponReceiver(_JobBoundReceiver):
    _approval_check: Callable[[], None]

    def _authorize(self, db: sqlite3.Connection, request: DispatchRequest) -> None:
        super()._authorize(db, request)
        self._approval_check()

    def _validate_payload(self, request: DispatchRequest, payload: BaseModel) -> None:
        validate_dispatch_payload(request, payload, schema=MaterializeCouponBatchArgs)
        args = MaterializeCouponBatchArgs.model_validate(payload.model_dump(), strict=True)
        if request.operation.resource_ref != f"coupon_batch:{args.coupon_batch_id}":
            raise DispatchRejected("reference coupon resource differs")


async def execute_approved_reference(
    leases: JobLeaseService,
    databases: DatabaseResources,
    path: Path,
    token: JobLeaseToken,
    args: MaterializeCouponBatchArgs,
    plan: LaunchPlan,
    approval_id: str,
    *,
    lookup_only: bool = False,
) -> DispatchObservation:
    """Hold platform/approval and business-fact locks through reference commit.

    The path is trusted test configuration, not a request parameter. The only effect
    is synthetic acceptance in its own database, explicitly separate from Oria DBs.
    """
    execution = JobExecutionService(leases, databases)
    writer = JobCouponDispatchWriter(databases.business_sessions)
    for sessions in (databases.platform_sessions, databases.business_sessions):
        database = sessions.kw["bind"].url.database
        if database is None or Path(database).resolve() == path.resolve():
            raise ValueError("reference history requires a separate file")
    token = JobLeaseToken.model_validate(token.model_dump(), strict=True)
    args = MaterializeCouponBatchArgs.model_validate(args.model_dump(), strict=True)
    plan = LaunchPlan.model_validate(plan.model_dump(), strict=True)
    job, ctx, guard = await leases._leased(token, business_resource_id=args.campaign_id)
    suffix = job.job_id.removeprefix("job_")
    if not isinstance(job.submission.spec, CampaignJobSpec) or (
        args.campaign_id,
        args.coupon_batch_id,
    ) != (f"campaign_{suffix}", f"coupon_{suffix}"):
        raise PermissionError("reference coupon must belong to campaign Job")
    policy_request = AuthorizationRequest(
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
    launch_guard = _LeaseAuthorization(leases, job, ctx, (policy_request,))
    launch_guard._check(policy_request, await leases._policy.authorize(policy_request, ctx))

    async def execute() -> DispatchObservation:
        async with leases._repository._hold_execution_lease(job, token, leases._now) as platform:
            approval = await execution._approvals.bind(platform).get(job.tenant_id, approval_id)
            if approval is None:
                raise PermissionError("reference launch approval unavailable")
            resume = ApprovalResumeRequest(
                approval_id=approval_id,
                approval_action="launch_approval",
                tool_name=LAUNCH_PLAN_TOOL_NAME,
                canonical_args_hash=plan.plan_hash,
                checkpoint_id=writer._facts.launch_saga(job, args).checkpoint,
            )

            def check_approval() -> None:
                launch_guard.check_commit()
                current = leases._policy.evaluate_now(policy_request, ctx)
                rejection = approval_resume_rejection(
                    approval,
                    resume,
                    LAUNCH_PLAN_POLICY,
                    policy_version=current.policy_version,
                    now=leases._now(),
                )
                if rejection is not None or (
                    approval.requester != job.requester_subject_id
                    or approval.decider == approval.requester
                    or approval.business_binding is not None
                ):
                    raise PermissionError("reference launch approval binding invalid")

            check_approval()
            async with databases.business_sessions() as business:
                try:
                    # load takes a Business write lock before reading any domain fact.
                    reservation = await writer.load(business, job, args, plan)
                    if reservation.status != "reserved":
                        raise PermissionError("reference cannot mix with real dispatch history")
                    await guard()
                    await launch_guard()
                    check_approval()
                    request = DispatchRequest(
                        operation=DispatchOperation(
                            tenant_id=job.tenant_id,
                            job_id=job.job_id,
                            adapter_id="fixture-coupon",
                            tool_name=MATERIALIZE_TOOL_NAME,
                            operation_key=f"{args.coupon_batch_id}:materialize:v1",
                            resource_ref=f"coupon_batch:{args.coupon_batch_id}",
                            tool_schema_version=1,
                            canonical_args_hash=reservation.canonical_args_hash,
                        ),
                        fence=token,
                    )
                    receiver = _ApprovedCouponReceiver(path, leases, job, guard)
                    receiver._approval_check = check_approval
                    try:
                        return receiver.execute(request, None if lookup_only else args)
                    finally:
                        receiver._active = False
                finally:
                    # No Oria business projection or state transition is committed.
                    await business.rollback()

    return await leases._write(execute())
