"""Validated event union accepted from authenticated integration adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol, TypeAlias, cast

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from oria.core.types import JsonValue, ValueModel

IntegrationEventType: TypeAlias = Literal[
    "merchant.enrollment_upserted",
    "enrollment.window_closed",
    "selection.decision_recorded",
    "selection.completed",
]
InboxProcessingStatus: TypeAlias = Literal[
    "matched",
    "consumed",
    "unauthorized",
    "no_wait",
    "type_mismatch",
    "resource_mismatch",
    "stale",
    "out_of_order",
    "wait_expired",
]
InboxResultStatus: TypeAlias = InboxProcessingStatus | Literal["duplicate", "invalid_envelope"]


class MerchantEnrollmentUpsertedPayload(ValueModel):
    campaign_id: str = Field(min_length=1)
    enrollment_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    product_version: str = Field(min_length=1)


class EnrollmentWindowClosedPayload(ValueModel):
    campaign_id: str = Field(min_length=1)
    enrollment_window_ref: str = Field(min_length=1)


class SelectionDecisionRecordedPayload(ValueModel):
    campaign_id: str = Field(min_length=1)
    submission_version: str = Field(min_length=1)
    selection_version: str = Field(min_length=1)
    enrollment_item_id: str = Field(min_length=1)
    decision: Literal["selected", "rejected"]
    reason_code: str | None = None

    @model_validator(mode="after")
    def require_rejection_reason(self) -> SelectionDecisionRecordedPayload:
        if self.decision == "rejected" and not self.reason_code:
            raise ValueError("rejected selection decision requires a reason code")
        return self


class SelectionCompletedPayload(ValueModel):
    campaign_id: str = Field(min_length=1)
    submission_version: str = Field(min_length=1)
    selection_version: str = Field(min_length=1)


class _IntegrationEventBase(ValueModel):
    schema_version: Literal[1] = 1
    tenant_id: str = Field(min_length=1, repr=False)
    adapter_id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    signature_subject: str = Field(min_length=1, repr=False)
    version: int = Field(ge=1)


class MerchantEnrollmentUpserted(_IntegrationEventBase):
    event_type: Literal["merchant.enrollment_upserted"]
    payload: MerchantEnrollmentUpsertedPayload


class EnrollmentWindowClosed(_IntegrationEventBase):
    event_type: Literal["enrollment.window_closed"]
    payload: EnrollmentWindowClosedPayload


class SelectionDecisionRecorded(_IntegrationEventBase):
    event_type: Literal["selection.decision_recorded"]
    payload: SelectionDecisionRecordedPayload


class SelectionCompleted(_IntegrationEventBase):
    event_type: Literal["selection.completed"]
    payload: SelectionCompletedPayload


IntegrationEventEnvelope: TypeAlias = Annotated[
    MerchantEnrollmentUpserted
    | EnrollmentWindowClosed
    | SelectionDecisionRecorded
    | SelectionCompleted,
    Field(discriminator="event_type"),
]

INTEGRATION_EVENT_ADAPTER: TypeAdapter[IntegrationEventEnvelope] = TypeAdapter(
    IntegrationEventEnvelope
)


def parse_integration_event(value: object) -> IntegrationEventEnvelope:
    """Parse only the four V0.3 integration event shapes."""
    return INTEGRATION_EVENT_ADAPTER.validate_python(value)


def integration_payload_hash(event: IntegrationEventEnvelope) -> str:
    payload = event.payload.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ExternalWait(ValueModel):
    """Trusted wait binding; inbound requests cannot supply this value."""

    tenant_id: str = Field(min_length=1, repr=False)
    wait_id: str = Field(min_length=1)
    event_type: IntegrationEventType
    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    checkpoint_id: str = Field(min_length=1)
    expires_at: datetime
    timeout_action: Literal["queue", "fail", "cancel"]
    status: Literal["waiting", "matched", "expired", "cancelled"] = "waiting"
    created_at: datetime
    resolved_at: datetime | None = None

    @field_validator("expires_at", "created_at", "resolved_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None, info: object) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> ExternalWait:
        if self.expires_at <= self.created_at:
            raise ValueError("external wait expiry must follow creation")
        if (self.status == "waiting") == (self.resolved_at is not None):
            raise ValueError("external wait resolution time must match its status")
        return self


class IntegrationInboxRecord(ValueModel):
    tenant_id: str = Field(min_length=1, repr=False)
    adapter_id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    schema_version: Literal[1] = 1
    event_type: IntegrationEventType
    resource_version: int = Field(ge=1)
    signature_subject: str = Field(min_length=1, repr=False)
    redacted_payload: dict[str, JsonValue] = Field(repr=False)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    processing_status: InboxProcessingStatus
    wait_id: str | None = None
    received_at: datetime
    processed_at: datetime

    @field_validator("received_at", "processed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("inbox timestamps must include a timezone")
        return value


class IntegrationInboxIdentity(ValueModel):
    """Opaque lookup identity passed from the trusted integration service."""

    tenant_id: str = Field(min_length=1, repr=False)
    adapter_id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)


class ConsumedIntegrationInbox(ValueModel):
    """Persisted inbox and wait binding after an atomic matched-to-consumed CAS."""

    record: IntegrationInboxRecord
    wait: ExternalWait

    @model_validator(mode="after")
    def require_consumed_binding(self) -> ConsumedIntegrationInbox:
        if (
            self.record.processing_status != "consumed"
            or self.record.tenant_id != self.wait.tenant_id
            or self.record.wait_id != self.wait.wait_id
        ):
            raise ValueError("consumed inbox does not match its trusted wait")
        return self


class InboxProcessResult(ValueModel):
    status: InboxResultStatus
    resume_eligible: bool
    record: IntegrationInboxRecord | None = None

    @model_validator(mode="after")
    def validate_resume_status(self) -> InboxProcessResult:
        if self.resume_eligible != (self.status == "matched"):
            raise ValueError("only a matched event can be eligible for resume")
        return self


class IntegrationEventInboxRepository(Protocol):
    async def add_wait(self, wait: ExternalWait) -> None: ...

    async def get_wait(self, tenant_id: str, wait_id: str) -> ExternalWait | None: ...

    async def add(self, record: IntegrationInboxRecord) -> bool: ...

    async def get(
        self,
        tenant_id: str,
        adapter_id: str,
        source_event_id: str,
    ) -> IntegrationInboxRecord | None: ...


class TrustedIntegrationEventInboxRepository(IntegrationEventInboxRepository, Protocol):
    """Inbox repository capable of atomically claiming a matched trusted event."""

    async def consume_matched(
        self,
        identity: IntegrationInboxIdentity,
        event: IntegrationEventEnvelope,
        *,
        consumed_at: datetime,
    ) -> ConsumedIntegrationInbox: ...


_REDACTED = "[REDACTED]"
_SENSITIVE_PAYLOAD_FIELDS = frozenset(
    {
        "authorization",
        "body",
        "cookie",
        "credential",
        "merchant_id",
        "password",
        "raw_body",
        "secret",
        "token",
    }
)


def _redact_payload(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            {
                str(key): (
                    _REDACTED
                    if str(key).lower() in _SENSITIVE_PAYLOAD_FIELDS
                    else _redact_payload(item)
                )
                for key, item in value.items()
            },
        )
    if isinstance(value, list):
        return cast(JsonValue, [_redact_payload(item) for item in value])
    return value


class IntegrationEventInboxService:
    """Persist one sanitized event and classify whether it exactly matches a trusted wait."""

    def __init__(
        self,
        repository: IntegrationEventInboxRepository,
        *,
        authorized_subjects: Mapping[tuple[str, str], frozenset[str]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._authorized_subjects = dict(authorized_subjects)
        self._clock = clock

    async def register_wait(self, wait: ExternalWait) -> ExternalWait:
        """Persist a trusted wait idempotently before a graph can interrupt."""

        existing = await self._repository.get_wait(wait.tenant_id, wait.wait_id)
        if existing is not None:
            if existing != wait:
                raise PermissionError("external wait identity has a different frozen binding")
            return existing
        await self._repository.add_wait(wait)
        return wait

    async def process(
        self,
        value: object,
        *,
        wait: ExternalWait | None,
    ) -> InboxProcessResult:
        try:
            event = parse_integration_event(value)
        except ValidationError:
            return InboxProcessResult(status="invalid_envelope", resume_eligible=False)
        now = self._now()
        status = self._classify(event, wait, now)
        if status == "unauthorized":
            # An unauthenticated sender must not be able to reserve the durable
            # deduplication identity of a later authenticated source event.
            return InboxProcessResult(status=status, resume_eligible=False)
        payload = cast(dict[str, JsonValue], event.payload.model_dump(mode="json"))
        record = IntegrationInboxRecord(
            tenant_id=event.tenant_id,
            adapter_id=event.adapter_id,
            source_event_id=event.source_event_id,
            event_type=event.event_type,
            resource_version=event.version,
            signature_subject=event.signature_subject,
            redacted_payload=cast(dict[str, JsonValue], _redact_payload(payload)),
            payload_hash=integration_payload_hash(event),
            processing_status=status,
            wait_id=(
                wait.wait_id if wait is not None and wait.tenant_id == event.tenant_id else None
            ),
            received_at=now,
            processed_at=now,
        )
        inserted = await self._repository.add(record)
        if not inserted:
            return InboxProcessResult(status="duplicate", resume_eligible=False)
        persisted = await self._repository.get(
            event.tenant_id,
            event.adapter_id,
            event.source_event_id,
        )
        if persisted is None:
            raise RuntimeError("persisted integration event is unavailable")
        return InboxProcessResult(
            status=status,
            resume_eligible=status == "matched",
            record=persisted,
        )

    def _classify(
        self,
        event: IntegrationEventEnvelope,
        wait: ExternalWait | None,
        now: datetime,
    ) -> InboxProcessingStatus:
        authorized = self._authorized_subjects.get((event.tenant_id, event.adapter_id), frozenset())
        if event.signature_subject not in authorized:
            return "unauthorized"
        if wait is None or wait.status != "waiting":
            return "no_wait"
        if wait.tenant_id != event.tenant_id:
            return "resource_mismatch"
        if now >= wait.expires_at:
            return "wait_expired"
        if event.event_type != wait.event_type:
            return "type_mismatch"
        if wait.resource_type != "campaign" or event.payload.campaign_id != wait.resource_id:
            return "resource_mismatch"
        if event.version < wait.expected_version:
            return "stale"
        if event.version > wait.expected_version:
            return "out_of_order"
        return "matched"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("inbox clock must return a timezone-aware timestamp")
        return now
