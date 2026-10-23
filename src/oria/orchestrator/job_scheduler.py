"""Opt-in bounded scheduling loop; no Graph execution or business-effect retries."""

import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass

from oria.core.jobs import TERMINAL_JOB_STATUSES, JobConflict, JobUnavailable
from oria.orchestrator.job_leases import JobLeaseService


@dataclass(frozen=True)
class SchedulingResult:
    scanned: int = 0
    released: int = 0
    expired: int = 0
    skipped: int = 0
    denied: int = 0
    unavailable: int = 0


class JobScheduler:
    """Single-tenant maintenance using current requester/worker authorization.

    The in-process lock protects only the scan cursor. Every mutation still uses the
    repository's full-snapshot CAS and commit guards. API startup never creates this
    loop; a trusted host owns its resources, task and stop event explicitly.
    """

    def __init__(
        self, leases: JobLeaseService, *, batch_size: int = 100, interval_seconds: float = 1
    ) -> None:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 100
        ):
            raise ValueError("scheduler batch size must be 1-100")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or not 0 < interval_seconds <= 60
        ):
            raise ValueError("scheduler interval must be in (0, 60] seconds")
        self._leases = leases
        self._batch_size, self._interval = batch_size, interval_seconds
        self._cursor = ""
        self._lock = asyncio.Lock()
        self.last_result = SchedulingResult()

    async def run_once(self) -> SchedulingResult:
        """Process at most one page; a denied Job cannot starve later references."""
        async with self._lock:
            leases = self._leases
            candidates = await leases._repository.scheduling_candidates(
                leases._executor.tenant_id,
                after_job_id=self._cursor,
                limit=self._batch_size,
            )
            counts = dict(scanned=0, released=0, expired=0, skipped=0, denied=0, unavailable=0)
            for job_id in candidates:
                outcome = "skipped"
                try:
                    job = await leases._load(job_id)
                    now = leases._now()
                    if job.status not in TERMINAL_JOB_STATUSES:
                        cancelled_expired = (
                            job.status == "cancellation_requested"
                            and job.lease_expires_at is not None
                            and job.lease_expires_at <= now
                        )
                        if job.timeout_at <= now or cancelled_expired:
                            await leases.expire(job_id)
                            outcome = "expired"
                        elif (
                            job.status == "retry_scheduled"
                            and job.retry_at is not None
                            and job.retry_at <= now
                        ):
                            await leases.release_retry(job_id)
                            outcome = "released"
                except JobConflict:
                    # Another writer won, or the newly loaded row is no longer due.
                    outcome = "skipped"
                except PermissionError:
                    outcome = "denied"
                except JobUnavailable:
                    outcome = "unavailable"
                counts["scanned"] += 1
                counts[outcome] += 1
                self._cursor = job_id
            if len(candidates) < self._batch_size:
                self._cursor = ""
            self.last_result = SchedulingResult(**counts)
            return self.last_result

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until explicitly stopped; cancellation propagates to DB cleanup.

        Individual unavailable rows are counted by run_once. A scan/database outage
        also waits a full interval, avoiding a tight loop; no exception text is stored.
        """
        while not stop.is_set():
            try:
                await self.run_once()
            except JobUnavailable:
                self.last_result = SchedulingResult(unavailable=1)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
