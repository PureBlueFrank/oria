"""Bounded retry and expiry transitions; never authorize replay of business effects."""

from datetime import datetime
from typing import Literal, TypeAlias

from oria.core.job_leases import JobLeaseToken, validate_lease
from oria.core.jobs import TERMINAL_JOB_STATUSES, Job, JobConflict, validate_job_update

ScheduleOperation: TypeAlias = Literal["retry", "release_retry", "expire"]


def validate_schedule_time(
    job: Job,
    operation: ScheduleOperation,
    now: datetime,
    token: JobLeaseToken | None = None,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("schedule time must include timezone")
    if now < job.updated_at or job.status in TERMINAL_JOB_STATUSES:
        raise JobConflict("job cannot be scheduled")
    if operation == "retry":
        if token is None:
            raise JobConflict("retry requires the current lease")
        validate_lease(job, token, now)
        if job.status != "running" or job.cancel_requested or job.attempt >= job.max_attempts:
            raise JobConflict("job cannot schedule another attempt")
    elif operation == "release_retry":
        if (
            job.status != "retry_scheduled"
            or job.cancel_requested
            or job.retry_at is None
            or now < job.retry_at
            or now >= job.timeout_at
            or job.attempt < 1
            or job.attempt >= job.max_attempts
        ):
            raise JobConflict("job retry is not available")
    elif operation == "expire":
        cancelled_owner_expired = (
            job.status == "cancellation_requested"
            and job.cancel_requested
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if now < job.timeout_at and not cancelled_owner_expired:
            raise JobConflict("job has not expired")
    else:
        raise ValueError("unknown schedule operation")


def validate_schedule_change(
    expected: Job,
    replacement: Job,
    operation: ScheduleOperation,
    token: JobLeaseToken | None,
    now: datetime,
) -> None:
    validate_job_update(expected, replacement)
    validate_schedule_time(expected, operation, now, token)
    allowed = {"revision", "updated_at", "status", "retry_at"}
    if operation == "retry":
        allowed |= {"lease_owner", "lease_expires_at", "heartbeat_at"}
        if (
            replacement.status != "retry_scheduled"
            or replacement.retry_at is None
            or not now < replacement.retry_at < expected.timeout_at
        ):
            raise JobConflict("retry must fall before the absolute deadline")
    elif operation == "release_retry":
        allowed |= {"attempt"}
        if replacement.status != "queued" or replacement.attempt != expected.attempt + 1:
            raise JobConflict("retry release must reserve exactly one attempt")
    elif operation == "expire":
        allowed |= {"lease_owner", "lease_expires_at", "heartbeat_at"}
        status = "cancelled" if expected.cancel_requested else "failed"
        if replacement.status != status:
            raise JobConflict("invalid expired job completion")
    if any(
        getattr(expected, field) != getattr(replacement, field)
        for field in Job.model_fields
        if field not in allowed
    ):
        raise JobConflict("schedule operation changed unrelated job fields")
