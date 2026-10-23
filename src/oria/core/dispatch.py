"""Receiver-side dispatch contracts; no production adapter or authorization transport.

The validation helpers are pure checks, not a lock or an admission authority. A receiver
must serialize its current lease/authorization check with durable admission and enforce
the stable operation key at the actual effect boundary. See ADR-040.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, Self, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

from oria.core.approvals import canonical_args_hash
from oria.core.job_leases import JobLeaseToken, validate_lease
from oria.core.jobs import Job, JobConflict
from oria.core.types import ValueModel
from oria.domain.ledger import Receipt


class DispatchRejected(ValueError):
    """A receiver refused this admission; this says nothing about an earlier attempt."""


class DispatchOperation(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    adapter_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    operation_key: str = Field(min_length=1, repr=False)
    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$", repr=False)
    resource_ref: str = Field(min_length=1, repr=False)
    tool_schema_version: int = Field(ge=1, strict=True)
    canonical_args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", repr=False)

    @property
    def deduplication_key(self) -> tuple[str, str, str, str]:
        """Neither the payload hash, Job nor lease epoch creates a new operation."""
        return self.tenant_id, self.adapter_id, self.tool_name, self.operation_key


class DispatchRequest(ValueModel):
    operation: DispatchOperation
    fence: JobLeaseToken

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (self.operation.tenant_id, self.operation.job_id) != (
            self.fence.tenant_id,
            self.fence.job_id,
        ):
            raise ValueError("dispatch operation and fence scope differ")
        return self


class DispatchObservation(ValueModel):
    """A bound, redacted result; acceptance is not completion of a business effect."""

    operation: DispatchOperation
    state: Literal["pending", "succeeded", "rejected", "unknown", "not_found"]
    observed_at: datetime
    accepted_fence: JobLeaseToken | None = None
    receipt: Receipt | None = None

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dispatch observation time must include timezone")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.state in {"pending", "succeeded", "rejected"} and self.accepted_fence is None:
            raise ValueError("known dispatch admission requires its original fence")
        if self.state == "not_found" and self.accepted_fence is not None:
            raise ValueError("not_found cannot claim admission")
        if self.accepted_fence is not None and (
            self.accepted_fence.tenant_id,
            self.accepted_fence.job_id,
        ) != (self.operation.tenant_id, self.operation.job_id):
            raise ValueError("dispatch acceptance scope differs")
        if self.state in {"succeeded", "rejected"}:
            expected = "accepted" if self.state == "succeeded" else "rejected"
            if (
                self.receipt is None
                or self.receipt.status != expected
                or self.receipt.adapter_id != self.operation.adapter_id
                or self.receipt.resource_ref != self.operation.resource_ref
                or self.receipt.received_at > self.observed_at
            ):
                raise ValueError("dispatch outcome requires a matching final receipt")
        elif self.receipt is not None:
            raise ValueError("unresolved dispatch cannot contain a final receipt")
        return self


class DispatchAdmission(DispatchObservation):
    """Admission cannot report absence; a lost response is an unknown outcome."""

    state: Literal["pending", "succeeded", "rejected", "unknown"]


PayloadT = TypeVar("PayloadT", bound=BaseModel, contravariant=True)


class FencedDispatchAdapter(Protocol[PayloadT]):
    """A contract to implement and verify per target, not a capability declaration.

    Authentication, current Policy/approval and authoritative lease checks are mandatory
    at the receiver. ``admit`` atomically binds the stable key and normalized payload;
    duplicates return its history without another effect, even across lease epochs.
    ``lookup`` authenticates/authorizes, reads history, and never admits or retries work.
    A timeout, exception, lost response or not_found is never permission to redispatch.
    """

    async def admit(self, request: DispatchRequest, payload: PayloadT) -> DispatchAdmission: ...

    async def lookup(self, request: DispatchRequest) -> DispatchObservation: ...


def validate_dispatch_admission(request: DispatchRequest, current_job: Job, now: datetime) -> None:
    """Check a receiver-owned current row *inside* its atomic admission boundary.

    Callers must additionally verify current requester/worker Policy, approval, domain
    bindings and typed payload. Passing a worker-provided snapshot is not fencing.
    """
    request = DispatchRequest.model_validate(request.model_dump(warnings=False), strict=True)
    current_job = Job.model_validate(current_job.model_dump(warnings=False), strict=True)
    if current_job.status != "running" or current_job.cancel_requested:
        raise DispatchRejected("dispatch job is not running")
    try:
        validate_lease(current_job, request.fence, now)
    except JobConflict as exc:
        raise DispatchRejected("dispatch fence is not current") from exc


def validate_dispatch_payload(
    request: DispatchRequest, payload: BaseModel, *, schema: type[BaseModel]
) -> None:
    """Bind the actual typed payload using the same canonical hash as approvals."""
    request = DispatchRequest.model_validate(request.model_dump(warnings=False), strict=True)
    payload = schema.model_validate(payload.model_dump(mode="python", warnings=False), strict=True)
    actual = canonical_args_hash(
        tool_name=request.operation.tool_name,
        tool_schema_version=request.operation.tool_schema_version,
        schema=schema,
        args=payload.model_dump(mode="python"),
    )
    if actual != request.operation.canonical_args_hash:
        raise DispatchRejected("dispatch payload binding differs")


def validate_dispatch_history(request: DispatchRequest, history: DispatchObservation) -> None:
    """Reject any changed binding at the same durable key, including another Job."""
    request = DispatchRequest.model_validate(request.model_dump(warnings=False), strict=True)
    history = DispatchObservation.model_validate(history.model_dump(warnings=False), strict=True)
    if request.operation != history.operation:
        raise DispatchRejected("dispatch operation binding conflicts with history")
    accepted = history.accepted_fence
    if accepted is not None and (
        accepted.epoch > request.fence.epoch
        or (accepted.epoch == request.fence.epoch and accepted.owner != request.fence.owner)
    ):
        raise DispatchRejected("dispatch acceptance is ahead of the requesting fence")


def reconcile_dispatch(
    request: DispatchRequest, observation: DispatchObservation
) -> Literal["succeeded", "failed", "unknown"]:
    """Classify an executing/unknown ledger outcome; never return a retry grant.

    Reconciliation still needs current authorization and a bound, authenticated target
    response. This pure projection performs no ledger or domain writes.
    """
    # Frozen values still permit unchecked model_copy/model_construct; validate the
    # complete nested observation before turning unknown into a terminal result.
    observation = DispatchObservation.model_validate(
        observation.model_dump(warnings=False), strict=True
    )
    validate_dispatch_history(request, observation)
    if observation.state == "succeeded":
        return "succeeded"
    if observation.state == "rejected":
        return "failed"
    return "unknown"
