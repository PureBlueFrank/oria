"""Durable admission values and state transitions; execution belongs to workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from oria.analytics.models import AnalyticsPeriod
from oria.core.types import ValueModel
from oria.domain.business import EnrollmentMode

Code = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")]
SessionID = Annotated[str, Field(pattern=r"^session_[0-9a-f]{32}$")]
JobStatus: TypeAlias = Literal[
    "queued",
    "running",
    "waiting_approval",
    "waiting_event",
    "retry_scheduled",
    "cancellation_requested",
    "cancelled",
    "succeeded",
    "failed",
]
TERMINAL_JOB_STATUSES = frozenset({"cancelled", "succeeded", "failed"})


class JobConflict(ValueError):
    """An idempotency key or expected revision conflicts with durable state."""


class JobUnavailable(RuntimeError):
    """Safe persistence failure, without SQL, credentials or request contents."""


class CampaignJobSpec(ValueModel):
    scenario: Literal["campaign"] = "campaign"
    categories: tuple[Code, ...] = Field(min_length=1, max_length=10)
    cities: tuple[Code, ...] = Field(min_length=1, max_length=10)
    enrollment_mode: EnrollmentMode
    max_candidates: int = Field(default=10, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def unique_filters(self) -> CampaignJobSpec:
        if len(set(self.categories)) != len(self.categories) or len(set(self.cities)) != len(
            self.cities
        ):
            raise ValueError("campaign filters must be unique")
        return self


class AttributionJobSpec(ValueModel):
    scenario: Literal["attribution"] = "attribution"
    period: AnalyticsPeriod
    metric: Literal[
        "impressions",
        "visits",
        "enrollments",
        "confirmations",
        "redemptions",
        "visit_rate",
        "enrollment_rate",
        "confirmation_rate",
        "redemption_rate",
    ]
    region: Code | None = None
    category: Code | None = None


JobSpec: TypeAlias = Annotated[
    CampaignJobSpec | AttributionJobSpec, Field(discriminator="scenario")
]


class JobSubmission(ValueModel):
    session_id: SessionID
    spec: JobSpec


def submission_json(submission: JobSubmission) -> str:
    """Persist typed business slots, never raw chat prompts or authentication claims."""
    return json.dumps(
        submission.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def submission_hash(submission: JobSubmission) -> str:
    return hashlib.sha256(submission_json(submission).encode()).hexdigest()


class JobSession(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    session_id: SessionID
    requester_subject_id: str = Field(min_length=1, repr=False)
    requester_issuer: str = Field(min_length=1, repr=False)
    requester_external_subject: str = Field(min_length=1, repr=False)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session time must include timezone")
        return value


class Job(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    session_id: SessionID
    thread_id: str = Field(min_length=1, repr=False)
    run_id: str = Field(min_length=1, repr=False)
    requester_subject_id: str = Field(min_length=1, repr=False)
    requester_issuer: str = Field(min_length=1, repr=False)
    requester_external_subject: str = Field(min_length=1, repr=False)
    submission: JobSubmission = Field(repr=False)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    status: JobStatus = "queued"
    revision: int = Field(default=0, ge=0, strict=True)
    attempt: int = Field(default=0, ge=0, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    lease_owner: str | None = Field(default=None, min_length=1, repr=False)
    lease_epoch: int = Field(default=0, ge=0, strict=True)
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    retry_at: datetime | None = None
    timeout_at: datetime
    cancel_requested: bool = False
    schedule_policy: Literal["once"] = "once"
    webhook_id: str | None = Field(default=None, repr=False)
    accepted_checkpoint_ns: str | None = Field(default=None, min_length=1, repr=False)
    accepted_checkpoint_id: str | None = Field(default=None, min_length=1, repr=False)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "created_at", "updated_at", "timeout_at", "retry_at", "lease_expires_at", "heartbeat_at"
    )
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("job time must include timezone")
        return value

    @model_validator(mode="after")
    def valid_state(self) -> Job:
        if (
            self.submission.session_id != self.session_id
            or submission_hash(self.submission) != self.request_hash
        ):
            raise ValueError("job request snapshot mismatch")
        if self.updated_at < self.created_at or self.timeout_at <= self.created_at:
            raise ValueError("invalid job timestamps")
        if self.attempt > self.max_attempts:
            raise ValueError("job attempt budget exceeded")
        active = self.status in {"running", "cancellation_requested"}
        if active:
            if (
                self.lease_owner is None
                or self.lease_epoch < 1
                or self.lease_expires_at is None
                or self.heartbeat_at is None
                or self.attempt < 1
                or self.lease_expires_at <= self.heartbeat_at
            ):
                raise ValueError("active job requires a current lease")
        elif any(
            value is not None
            for value in (self.lease_owner, self.lease_expires_at, self.heartbeat_at)
        ):
            raise ValueError("inactive job cannot retain a lease")
        if self.status == "cancellation_requested" and not self.cancel_requested:
            raise ValueError("cancellation must be requested")
        if (self.status == "retry_scheduled") != (self.retry_at is not None):
            raise ValueError("retry schedule must match status")
        if (self.accepted_checkpoint_ns is None) != (self.accepted_checkpoint_id is None):
            raise ValueError("checkpoint pointer must be complete")
        if (
            self.status in {"waiting_approval", "waiting_event"}
            and self.accepted_checkpoint_id is None
        ):
            raise ValueError("waiting job requires an accepted checkpoint")
        return self


_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset(
        {
            "waiting_approval",
            "waiting_event",
            "retry_scheduled",
            "succeeded",
            "failed",
            "cancellation_requested",
        }
    ),
    "waiting_approval": frozenset({"queued", "cancelled", "failed"}),
    "waiting_event": frozenset({"queued", "cancelled", "failed"}),
    "retry_scheduled": frozenset({"queued", "cancelled", "failed"}),
    "cancellation_requested": frozenset({"cancelled", "failed"}),
}
_MUTABLE_JOB_FIELDS = frozenset(
    {
        "status",
        "revision",
        "attempt",
        "lease_owner",
        "lease_epoch",
        "lease_expires_at",
        "heartbeat_at",
        "retry_at",
        "cancel_requested",
        "accepted_checkpoint_ns",
        "accepted_checkpoint_id",
        "updated_at",
    }
)


def validate_job_update(expected: Job, replacement: Job) -> None:
    """Shared immutable bindings and monotonic counters for every CAS operation."""
    if replacement.revision != expected.revision + 1:
        raise JobConflict("invalid job revision")
    if replacement.updated_at < expected.updated_at:
        raise JobConflict("job time moved backwards")
    if any(
        getattr(expected, field) != getattr(replacement, field)
        for field in Job.model_fields
        if field not in _MUTABLE_JOB_FIELDS
    ):
        raise JobConflict("immutable job binding changed")
    if replacement.lease_epoch < expected.lease_epoch or replacement.attempt < expected.attempt:
        raise JobConflict("job attempt moved backwards")
    if expected.cancel_requested and not replacement.cancel_requested:
        raise JobConflict("job cancellation cannot be cleared")


def validate_transition(expected: Job, replacement: Job) -> None:
    validate_job_update(expected, replacement)
    if replacement.status not in _TRANSITIONS.get(expected.status, frozenset()):
        raise JobConflict("invalid job transition")
    if expected.status == "queued" and replacement.status == "running":
        if (
            replacement.attempt != max(1, expected.attempt)
            or replacement.lease_epoch != expected.lease_epoch + 1
        ):
            raise JobConflict("queued continuation must preserve the execution attempt")
    elif (replacement.attempt, replacement.lease_epoch) != (expected.attempt, expected.lease_epoch):
        raise JobConflict("job attempt can advance only when claimed")


class JobView(ValueModel):
    job_id: str
    session_id: str
    scenario: Literal["campaign", "attribution"]
    status: JobStatus
    revision: int
    attempt: int
    max_attempts: int
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime


def job_view(job: Job) -> JobView:
    return JobView(
        job_id=job.job_id,
        session_id=job.session_id,
        scenario=job.submission.spec.scenario,
        status=job.status,
        revision=job.revision,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
