"""Validated event union accepted from authenticated integration adapters."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from oria.core.types import ValueModel


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
