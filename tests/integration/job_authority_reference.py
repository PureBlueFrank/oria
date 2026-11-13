"""Test-only bridge from real Job/Policy authority to synthetic durable effects.

This deliberately does not implement FencedDispatchAdapter or authorize coupons:
launch approval and domain facts are not integrated. No runtime imports this module.
"""

import sqlite3
from pathlib import Path

from tests.contract.durable_dispatch_receiver import DurableReferenceReceiver
from tests.contract.test_v06_dispatch_contract import _Payload

from oria.core.dispatch import DispatchObservation, DispatchRequest, validate_dispatch_admission
from oria.core.job_leases import JobLeaseToken
from oria.core.jobs import Job
from oria.orchestrator.job_leases import JobLeaseService, _LeaseAuthorization


class _JobBoundReceiver(DurableReferenceReceiver):
    """One invocation only, inside the repository's held execution lease."""

    def __init__(self, path: Path, service: JobLeaseService, job: Job, guard: _LeaseAuthorization):
        self._service, self._job, self._guard = service, job, guard
        self._active = True
        super().__init__(path, clock=service._now)

    def replace_authority(self, job: Job, *, allowed: bool = True) -> None:
        raise PermissionError("reference bridge authority is owned by JobRepository")

    def _authorize(self, db: sqlite3.Connection, request: DispatchRequest) -> None:
        if not self._active:
            raise PermissionError("reference authority scope ended")
        self._guard.check_commit()
        validate_dispatch_admission(request, self._job, self._service._now())


async def execute_reference(
    service: JobLeaseService,
    path: Path,
    request: DispatchRequest,
    payload: _Payload | None = None,
) -> DispatchObservation:
    """Hold actual platform authority across a separate synthetic receiver commit.

    Not cross-database atomicity: receiver history survives platform lock release
    failure. Trusted test code supplies the path; no user-provided receiver callback.
    """
    request = DispatchRequest.model_validate(request.model_dump(), strict=True)
    token = JobLeaseToken.model_validate(request.fence.model_dump(), strict=True)
    campaign_id = f"campaign_{token.job_id.removeprefix('job_')}"
    coupon_id = f"coupon_{token.job_id.removeprefix('job_')}"
    if request.operation.resource_ref != f"coupon_batch:{coupon_id}":
        raise PermissionError("reference resource does not belong to Job")
    job, _, guard = await service._leased(token, business_resource_id=campaign_id)
    if job.submission.spec.scenario != "campaign":
        raise PermissionError("reference requires a campaign Job")
    platform_path = service._repository._sessions.kw["bind"].url.database
    if platform_path is None or Path(platform_path).resolve() == path.resolve():
        raise ValueError("reference history must use a separate SQLite file")

    async def execute() -> DispatchObservation:
        async with service._repository._hold_execution_lease(job, token, service._now):
            await guard()
            receiver = _JobBoundReceiver(path, service, job, guard)
            try:
                # Synchronous local-only effect; no await/thread/network escapes
                # between final authority check and receiver commit dispatch.
                return receiver.execute(request, payload)
            finally:
                receiver._active = False

    return await service._write(execute())
