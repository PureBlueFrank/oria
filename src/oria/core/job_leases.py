"""Lease proofs and narrowly allowed Job-row changes, separate from API cancellation."""

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import Field

from oria.core.jobs import Job, JobConflict, validate_job_update
from oria.core.types import ValueModel

LeaseOperation: TypeAlias = Literal["claim", "heartbeat", "checkpoint", "finish"]


class JobLeaseToken(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$", repr=False)
    owner: str = Field(min_length=1, repr=False)
    epoch: int = Field(ge=1, strict=True)

    @property
    def checkpoint_ns(self) -> str:
        return f"job:{self.job_id}:epoch:{self.epoch}"


def lease_token(job: Job) -> JobLeaseToken:
    if job.status not in {"running", "cancellation_requested"} or job.lease_owner is None:
        raise JobConflict("job has no active lease")
    return JobLeaseToken(
        tenant_id=job.tenant_id, job_id=job.job_id, owner=job.lease_owner, epoch=job.lease_epoch
    )


def validate_lease(job: Job, token: JobLeaseToken, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("lease time must include timezone")
    if (
        lease_token(job) != token
        or job.lease_expires_at is None
        or now < job.updated_at
        or now >= job.lease_expires_at
        or now >= job.timeout_at
    ):
        raise JobConflict("job lease is no longer valid")


def validate_claim(job: Job, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("lease time must include timezone")
    if (
        job.cancel_requested
        or (job.status == "running" and job.attempt >= job.max_attempts)
        or now < job.updated_at
        or now >= job.timeout_at
        or job.status not in {"queued", "running"}
        or (
            job.status == "running" and (job.lease_expires_at is None or job.lease_expires_at > now)
        )
    ):
        raise JobConflict("job cannot be claimed")


def validate_lease_change(
    expected: Job,
    replacement: Job,
    operation: LeaseOperation,
    token: JobLeaseToken | None,
) -> None:
    validate_job_update(expected, replacement)
    now = replacement.updated_at
    allowed = {"revision", "updated_at"}
    if operation == "claim":
        validate_claim(expected, now)
        allowed |= {
            "status",
            "attempt",
            "lease_epoch",
            "lease_owner",
            "heartbeat_at",
            "lease_expires_at",
        }
        if (
            replacement.status != "running"
            or replacement.attempt
            != (expected.attempt + 1 if expected.status == "running" else max(1, expected.attempt))
            or replacement.lease_epoch != expected.lease_epoch + 1
            or replacement.heartbeat_at != now
            or replacement.lease_expires_at is None
            or replacement.lease_expires_at > replacement.timeout_at
        ):
            raise JobConflict("invalid lease claim")
    else:
        if token is None:
            raise JobConflict("lease token is required")
        validate_lease(expected, token, now)
        if operation == "heartbeat":
            allowed |= {"heartbeat_at", "lease_expires_at"}
            if (
                replacement.heartbeat_at != now
                or replacement.lease_expires_at is None
                or expected.lease_expires_at is None
                or replacement.lease_expires_at < expected.lease_expires_at
                or replacement.lease_expires_at > replacement.timeout_at
            ):
                raise JobConflict("invalid lease heartbeat")
        elif operation == "checkpoint":
            allowed |= {"accepted_checkpoint_ns", "accepted_checkpoint_id"}
            if (
                expected.status != "running"
                or replacement.accepted_checkpoint_ns != token.checkpoint_ns
                or replacement.accepted_checkpoint_id is None
            ):
                raise JobConflict("invalid checkpoint publication")
        elif operation == "finish":
            allowed |= {"status", "lease_owner", "lease_expires_at", "heartbeat_at"}
            statuses = (
                {"cancelled", "failed"}
                if expected.status == "cancellation_requested"
                else {"succeeded", "failed", "waiting_approval", "waiting_event"}
            )
            if replacement.status not in statuses or (
                replacement.status in {"waiting_approval", "waiting_event"}
                and replacement.accepted_checkpoint_ns != token.checkpoint_ns
            ):
                raise JobConflict("invalid leased job completion")
        else:
            raise ValueError("unknown lease operation")
    if any(
        getattr(expected, field) != getattr(replacement, field)
        for field in Job.model_fields
        if field not in allowed
    ):
        raise JobConflict("lease operation changed unrelated job fields")
