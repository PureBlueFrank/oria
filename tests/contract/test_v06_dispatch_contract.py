"""Local receiver model only: no real target, persistence, or distributed guarantee."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from oria.core.approvals import canonical_args_hash
from oria.core.dispatch import (
    DispatchAdmission,
    DispatchObservation,
    DispatchOperation,
    DispatchRejected,
    DispatchRequest,
    FencedDispatchAdapter,
    reconcile_dispatch,
    validate_dispatch_admission,
    validate_dispatch_history,
    validate_dispatch_payload,
)
from oria.core.job_leases import lease_token
from oria.core.jobs import CampaignJobSpec, Job, JobSubmission, submission_hash
from oria.core.types import ValueModel
from oria.domain.ledger import Receipt

pytestmark = [pytest.mark.contract, pytest.mark.security, pytest.mark.recovery]
NOW = datetime(2026, 10, 22, tzinfo=UTC)


class _Payload(ValueModel):
    coupon_id: str
    amount: Decimal


def _job(**updates: object) -> Job:
    submission = JobSubmission(
        session_id=f"session_{'1' * 32}",
        spec=CampaignJobSpec(categories=("food",), cities=("shanghai",), enrollment_mode="auto"),
    )
    job = Job(
        tenant_id="tenant-a",
        job_id=f"job_{'1' * 32}",
        session_id=submission.session_id,
        thread_id="thread-a",
        run_id="run-a",
        requester_subject_id="actor-a",
        requester_issuer="fixture-issuer",
        requester_external_subject="external-a",
        submission=submission,
        request_hash=submission_hash(submission),
        idempotency_key="a" * 64,
        status="running",
        attempt=1,
        lease_owner="worker-a:instance-a",
        lease_epoch=1,
        lease_expires_at=NOW + timedelta(minutes=1),
        heartbeat_at=NOW,
        timeout_at=NOW + timedelta(hours=1),
        created_at=NOW,
        updated_at=NOW,
    )
    return Job.model_validate(job.model_dump() | updates)


def _request(job: Job | None = None, payload: _Payload | None = None) -> DispatchRequest:
    job = job or _job()
    payload = payload or _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))
    return DispatchRequest(
        operation=DispatchOperation(
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            adapter_id="fixture-coupon",
            tool_name="materialize_coupon_batch",
            operation_key="coupon-a:materialize:v1",
            resource_ref=f"coupon_batch:{payload.coupon_id}",
            tool_schema_version=1,
            canonical_args_hash=canonical_args_hash(
                tool_name="materialize_coupon_batch",
                tool_schema_version=1,
                schema=_Payload,
                args=payload.model_dump(),
            ),
        ),
        fence=lease_token(job),
    )


def _receipt(request: DispatchRequest, status: Literal["accepted", "rejected"]) -> Receipt:
    return Receipt(
        receipt_id="fixture-receipt-a",
        adapter_id=request.operation.adapter_id,
        resource_ref=request.operation.resource_ref,
        status=status,
        received_at=NOW,
        summary_hash=f"sha256:{'a' * 64}",
    )


class _Receiver:
    """An in-process conformance fixture, deliberately unavailable to runtime assembly.

    One fixture lock owns both authority replacement and acceptance. A real target must
    prove this linearization across its actual processes and durable effect boundary.
    """

    def __init__(self) -> None:
        self.job = _job()
        self.now = NOW
        self.authorized = True
        self._lock = asyncio.Lock()
        self.history: dict[tuple[str, str, str, str], DispatchAdmission] = {}
        self.effect_count = 0
        self.lose_response = False

    async def replace_authority(self, job: Job) -> None:
        async with self._lock:
            self.job = job

    def _authorize(self, request: DispatchRequest) -> None:
        if not self.authorized:
            raise PermissionError("fixture receiver denied")
        validate_dispatch_admission(request, self.job, self.now)

    async def admit(self, request: DispatchRequest, payload: _Payload) -> DispatchAdmission:
        async with self._lock:
            self._authorize(request)
            validate_dispatch_payload(request, payload, schema=_Payload)
            prior = self.history.get(request.operation.deduplication_key)
            if prior is not None:
                validate_dispatch_history(request, prior)
                return prior
            result = DispatchAdmission(
                operation=request.operation,
                state="pending",
                accepted_fence=request.fence,
                observed_at=self.now,
            )
            self.history[request.operation.deduplication_key] = result
            self.effect_count += 1
            if self.lose_response:
                raise TimeoutError("fixture dropped admission response")
            return result

    async def lookup(self, request: DispatchRequest) -> DispatchObservation:
        async with self._lock:
            self._authorize(request)
            prior = self.history.get(request.operation.deduplication_key)
            if prior is not None:
                validate_dispatch_history(request, prior)
                return prior
            return DispatchObservation(
                operation=request.operation, state="not_found", observed_at=self.now
            )

    async def settle(self, request: DispatchRequest, state: Literal["succeeded", "rejected"]):
        async with self._lock:
            prior = self.history[request.operation.deduplication_key]
            assert prior.state == "pending"
            self.history[request.operation.deduplication_key] = DispatchAdmission(
                operation=prior.operation,
                state=state,
                accepted_fence=prior.accepted_fence,
                receipt=_receipt(request, "accepted" if state == "succeeded" else "rejected"),
                observed_at=self.now,
            )


@pytest.fixture
def payload():
    return _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))


@pytest.mark.asyncio
async def test_current_authority_rejects_old_epoch_before_any_new_request(payload):
    receiver = _Receiver()
    old = _request()
    successor = _job(lease_epoch=2, lease_owner="worker-b:instance-b")
    await receiver.replace_authority(successor)
    with pytest.raises(DispatchRejected):
        await receiver.admit(old, payload)
    assert receiver.effect_count == 0
    assert receiver.history == {}
    result = await receiver.admit(_request(successor), payload)
    assert result.state == "pending" and receiver.effect_count == 1


@pytest.mark.asyncio
async def test_same_operation_is_admitted_once_across_duplicates_and_epochs(payload):
    receiver = _Receiver()
    adapter: FencedDispatchAdapter[_Payload] = receiver
    request = _request()
    first, duplicate = await asyncio.gather(
        adapter.admit(request, payload), adapter.admit(request, payload)
    )
    assert duplicate == first
    successor = _job(lease_epoch=2, lease_owner="worker-b:instance-b")
    await receiver.replace_authority(successor)
    repeated = await adapter.admit(_request(successor), payload)
    assert repeated == first and repeated.accepted_fence == request.fence
    with pytest.raises(DispatchRejected):
        await adapter.admit(request, payload)
    assert receiver.effect_count == 1


@pytest.mark.asyncio
async def test_changed_payload_same_stable_key_conflicts(payload):
    receiver = _Receiver()
    request = _request()
    await receiver.admit(request, payload)
    changed = _Payload(coupon_id="coupon-a", amount=Decimal("999"))
    changed_request = _request(payload=changed)
    assert request.operation.deduplication_key == changed_request.operation.deduplication_key
    with pytest.raises(DispatchRejected, match="binding conflicts"):
        await receiver.admit(changed_request, changed)
    assert receiver.effect_count == 1


@pytest.mark.asyncio
async def test_actual_payload_is_checked_before_first_admission(payload):
    receiver = _Receiver()
    with pytest.raises(DispatchRejected, match="payload binding"):
        await receiver.admit(_request(), _Payload(coupon_id="coupon-a", amount=Decimal("999")))
    assert receiver.effect_count == 0 and receiver.history == {}


@pytest.mark.asyncio
async def test_job_identity_cannot_create_a_second_effect_at_same_key(payload):
    receiver = _Receiver()
    await receiver.admit(_request(), payload)
    another_job = _job(job_id=f"job_{'2' * 32}")
    await receiver.replace_authority(another_job)
    with pytest.raises(DispatchRejected):
        await receiver.admit(_request(another_job), payload)
    assert receiver.effect_count == 1


@pytest.mark.asyncio
async def test_tenant_scope_keeps_independent_business_operations_separate(payload):
    receiver = _Receiver()
    first = await receiver.admit(_request(), payload)
    other_tenant = _job(tenant_id="tenant-b")
    await receiver.replace_authority(other_tenant)
    second = await receiver.admit(_request(other_tenant), payload)
    assert first.operation.deduplication_key != second.operation.deduplication_key
    assert receiver.effect_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "rejected"])
async def test_lost_response_reconciles_by_lookup_without_redispatch(payload, state):
    receiver = _Receiver()
    request = _request()
    receiver.lose_response = True
    with pytest.raises(TimeoutError):
        await receiver.admit(request, payload)
    assert reconcile_dispatch(request, await receiver.lookup(request)) == "unknown"
    await receiver.settle(request, state)
    result = await receiver.lookup(request)
    assert reconcile_dispatch(request, result) == (
        "succeeded" if state == "succeeded" else "failed"
    )
    assert receiver.effect_count == 1
    assert await receiver.admit(request, payload) == result
    assert receiver.effect_count == 1


@pytest.mark.asyncio
async def test_not_found_is_not_permission_to_submit_or_clear_unknown(payload):
    receiver = _Receiver()
    request = _request()
    observed = await receiver.lookup(request)
    assert observed.state == "not_found"
    assert reconcile_dispatch(request, observed) == "unknown"
    assert receiver.effect_count == 0 and receiver.history == {}


@pytest.mark.asyncio
async def test_unknown_observation_cannot_reenter_execution(payload):
    receiver = _Receiver()
    request = _request()
    await receiver.admit(request, payload)
    unknown = DispatchAdmission(
        operation=request.operation,
        state="unknown",
        accepted_fence=request.fence,
        observed_at=NOW,
    )
    receiver.history[request.operation.deduplication_key] = unknown
    assert reconcile_dispatch(request, await receiver.lookup(request)) == "unknown"
    assert await receiver.admit(request, payload) == unknown
    assert receiver.effect_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("already_admitted", [False, True])
@pytest.mark.parametrize("reason", ["expired", "cancelled", "revoked", "wrong_owner"])
async def test_current_authority_is_checked_before_new_or_historical_result(
    payload, already_admitted, reason
):
    receiver = _Receiver()
    request = _request()
    if already_admitted:
        await receiver.admit(request, payload)
    if reason == "expired":
        receiver.now = NOW + timedelta(minutes=1)
    elif reason == "cancelled":
        await receiver.replace_authority(
            _job(status="cancellation_requested", cancel_requested=True)
        )
    elif reason == "revoked":
        receiver.authorized = False
    else:
        await receiver.replace_authority(_job(lease_owner="worker-b:instance-b"))
    with pytest.raises((DispatchRejected, PermissionError)):
        await receiver.admit(request, payload)
    with pytest.raises((DispatchRejected, PermissionError)):
        await receiver.lookup(request)
    assert receiver.effect_count == int(already_admitted)


@pytest.mark.parametrize("state", ["pending", "succeeded", "rejected"])
def test_known_admission_requires_original_fence(state):
    with pytest.raises(ValidationError, match="original fence"):
        DispatchObservation(operation=_request().operation, state=state, observed_at=NOW)


def test_admission_cannot_report_not_found():
    with pytest.raises(ValidationError):
        DispatchAdmission(operation=_request().operation, state="not_found", observed_at=NOW)


@pytest.mark.parametrize("mutation", ["adapter", "resource", "status", "time", "missing"])
def test_terminal_outcome_requires_matching_final_receipt(mutation):
    request = _request()
    receipt = _receipt(request, "accepted")
    changes = {
        "adapter": {"adapter_id": "other-adapter"},
        "resource": {"resource_ref": "coupon_batch:another"},
        "status": {"status": "unknown"},
        "time": {"received_at": NOW + timedelta(seconds=1)},
    }
    if mutation == "missing":
        receipt = None
    else:
        receipt = Receipt.model_validate(receipt.model_dump() | changes[mutation])
    with pytest.raises(ValidationError, match="matching final receipt"):
        DispatchObservation(
            operation=request.operation,
            state="succeeded",
            accepted_fence=request.fence,
            observed_at=NOW,
            receipt=receipt,
        )


@pytest.mark.parametrize("mutation", ["hash", "resource", "job", "adapter", "schema"])
def test_reconciliation_rejects_mismatched_operation(mutation):
    request = _request()
    updates = {
        "hash": {"canonical_args_hash": f"sha256:{'b' * 64}"},
        "resource": {"resource_ref": "coupon_batch:other"},
        "job": {"job_id": f"job_{'2' * 32}"},
        "adapter": {"adapter_id": "another"},
        "schema": {"tool_schema_version": 2},
    }
    operation = DispatchOperation.model_validate(request.operation.model_dump() | updates[mutation])
    observation = DispatchObservation(operation=operation, state="unknown", observed_at=NOW)
    with pytest.raises(DispatchRejected):
        reconcile_dispatch(request, observation)


@pytest.mark.parametrize("change", [{"epoch": 2}, {"owner": "other-owner"}])
def test_future_or_conflicting_acceptance_cannot_resolve_unknown(change):
    request = _request()
    observation = DispatchObservation(
        operation=request.operation,
        state="pending",
        observed_at=NOW,
        accepted_fence=request.fence.model_copy(update=change),
    )
    with pytest.raises(DispatchRejected):
        reconcile_dispatch(request, observation)


@pytest.mark.parametrize("field", ["tenant_id", "job_id"])
def test_request_cannot_mix_fence_scope(field):
    request = _request()
    changed = "another-tenant" if field == "tenant_id" else f"job_{'2' * 32}"
    with pytest.raises(ValidationError, match="scope differ"):
        DispatchRequest(
            operation=request.operation,
            fence=request.fence.model_copy(update={field: changed}),
        )


@pytest.mark.parametrize("state", ["succeeded", "rejected"])
def test_reconciliation_revalidates_copied_unknown_before_projecting_terminal_state(state):
    request = _request()
    unknown = DispatchObservation(operation=request.operation, state="unknown", observed_at=NOW)
    forged = unknown.model_copy(update={"state": state})
    with pytest.raises(ValidationError):
        reconcile_dispatch(request, forged)


@pytest.mark.parametrize("helper", ["admission", "payload", "history", "reconcile"])
def test_helpers_revalidate_copied_request_scope(helper, payload):
    request = _request()
    forged = request.model_copy(
        update={"operation": request.operation.model_copy(update={"tenant_id": "other-tenant"})}
    )
    history = DispatchObservation(operation=request.operation, state="unknown", observed_at=NOW)
    with pytest.raises(ValidationError):
        if helper == "admission":
            validate_dispatch_admission(forged, _job(), NOW)
        elif helper == "payload":
            validate_dispatch_payload(forged, payload, schema=_Payload)
        elif helper == "history":
            validate_dispatch_history(forged, history)
        else:
            reconcile_dispatch(forged, history)


def test_admission_revalidates_receiver_job_snapshot():
    job = _job().model_copy(update={"request_hash": "f" * 64})
    with pytest.raises(ValidationError):
        validate_dispatch_admission(_request(), job, NOW)


def test_reconciliation_revalidates_nested_receipt():
    request = _request()
    observation = DispatchObservation(
        operation=request.operation,
        state="succeeded",
        accepted_fence=request.fence,
        observed_at=NOW,
        receipt=_receipt(request, "accepted"),
    )
    forged = observation.model_copy(
        update={"receipt": observation.receipt.model_copy(update={"receipt_id": ""})}
    )
    with pytest.raises(ValidationError):
        reconcile_dispatch(request, forged)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["operation_key", "adapter_id"])
@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
async def test_copied_key_cannot_be_coerced_into_a_second_admission(payload, field):
    receiver = _Receiver()
    request = _request()
    await receiver.admit(request, payload)
    copied = request.model_copy(
        update={
            "operation": request.operation.model_copy(
                update={field: getattr(request.operation, field).encode()}
            )
        }
    )
    with pytest.raises(ValidationError):
        await receiver.admit(copied, payload)
    assert receiver.effect_count == 1


@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
def test_payload_validation_does_not_coerce_an_unchecked_copy(payload):
    copied = payload.model_copy(update={"amount": "10.00"})
    with pytest.raises(ValidationError):
        validate_dispatch_payload(_request(), copied, schema=_Payload)
