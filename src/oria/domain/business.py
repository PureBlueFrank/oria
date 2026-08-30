"""Immutable V0.3 merchant-recruitment business values and state machines."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from oria.core.types import JsonValue, ValueModel

EnrollmentMode = Literal["merchant", "auto", "hybrid"]
EnrollmentSource = Literal["merchant", "auto"]
CampaignStatus = Literal[
    "draft",
    "pending_launch_approval",
    "recruiting",
    "selecting",
    "pending_consumer_publish",
    "active",
    "completed",
    "cancelled",
]
CouponBatchStatus = Literal["draft", "materializing", "ready", "failed", "unknown", "expired"]
BusinessKey = tuple[str, ...]

_CAMPAIGN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_launch_approval"}),
    "pending_launch_approval": frozenset({"recruiting"}),
    "recruiting": frozenset({"selecting"}),
    "selecting": frozenset({"pending_consumer_publish"}),
    "pending_consumer_publish": frozenset({"active"}),
    "active": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
_COUPON_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"materializing"}),
    "materializing": frozenset({"ready", "failed", "unknown"}),
    "ready": frozenset({"expired"}),
    "failed": frozenset({"expired"}),
    "unknown": frozenset({"expired"}),
    "expired": frozenset(),
}


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


def _transition(
    current: str,
    target: str,
    transitions: dict[str, frozenset[str]],
) -> None:
    if target not in transitions[current]:
        raise ValueError(f"illegal state transition: {current} -> {target}")


class BusinessEntity(ValueModel):
    """Tenant-owned immutable entity with optimistic-lock and audit timestamps."""

    unique_key_fields: ClassVar[tuple[str, ...]]

    tenant_id: str = Field(min_length=1, repr=False)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "timestamp"))
        return _require_aware(value, field_name)

    @model_validator(mode="after")
    def validate_timestamp_order(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self

    def unique_key(self) -> BusinessKey:
        return tuple(str(getattr(self, name)) for name in self.unique_key_fields)

    def validate_tenant_links(self, *related: BusinessEntity) -> None:
        if any(item.tenant_id != self.tenant_id for item in related):
            raise ValueError("cross-tenant business association is forbidden")

    def _next_version(self, *, updated_at: datetime, **updates: object) -> Self:
        _require_aware(updated_at, "updated_at")
        if updated_at < self.updated_at:
            raise ValueError("updated_at must not move backwards")
        return self.model_copy(
            update={"version": self.version + 1, "updated_at": updated_at, **updates}
        )


class ProductSnapshot(BusinessEntity):
    unique_key_fields = ("tenant_id", "product_ref", "product_version")

    product_snapshot_id: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    catalog_snapshot_id: str = Field(min_length=1)
    attributes: dict[str, JsonValue]


class CampaignRuleSnapshotRef(BusinessEntity):
    """Business-side immutable reference; rule payload remains owned by RAG."""

    unique_key_fields = ("tenant_id", "snapshot_id", "snapshot_hash")

    campaign_rule_snapshot_ref_id: str = Field(min_length=1)
    snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Campaign(BusinessEntity):
    unique_key_fields = ("tenant_id", "campaign_id")

    campaign_id: str = Field(min_length=1)
    rule_snapshot_ref_id: str = Field(min_length=1)
    enrollment_mode: EnrollmentMode
    status: CampaignStatus = "draft"

    def transition_to(self, target: CampaignStatus, *, updated_at: datetime) -> Campaign:
        _transition(self.status, target, _CAMPAIGN_TRANSITIONS)
        return self._next_version(updated_at=updated_at, status=target)


class CouponBatch(BusinessEntity):
    unique_key_fields = ("tenant_id", "campaign_id", "coupon_spec_hash")

    coupon_batch_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    coupon_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: CouponBatchStatus = "draft"

    def transition_to(self, target: CouponBatchStatus, *, updated_at: datetime) -> CouponBatch:
        _transition(self.status, target, _COUPON_BATCH_TRANSITIONS)
        return self._next_version(updated_at=updated_at, status=target)


class LaunchSagaState(BusinessEntity):
    unique_key_fields = ("tenant_id", "campaign_id")

    launch_saga_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    status: Literal["pending", "running", "completed", "failed", "unknown"]
    checkpoint: str = Field(min_length=1)


class RecruitmentPublication(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "campaign_id",
        "merchant_scope_hash",
        "material_version",
    )

    recruitment_publication_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    merchant_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    material_version: str = Field(min_length=1)
    status: Literal["pending", "published", "failed", "unknown"]
    request_id: str | None = None
    receipt_id: str | None = None


class Enrollment(BusinessEntity):
    unique_key_fields = ("tenant_id", "campaign_id", "merchant_id")

    enrollment_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    mode: EnrollmentMode
    status: Literal["open", "submitted", "closed", "rejected"]


class EnrollmentItem(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "campaign_id",
        "merchant_id",
        "product_ref",
        "product_version",
    )

    enrollment_item_id: str = Field(min_length=1)
    enrollment_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    product_snapshot_id: str = Field(min_length=1)
    mode: EnrollmentMode
    sources: frozenset[EnrollmentSource] = Field(min_length=1)
    status: Literal["pending_confirmation", "confirmed", "rejected"]

    @model_validator(mode="after")
    def validate_sources_for_mode(self) -> Self:
        allowed: dict[str, frozenset[str]] = {
            "merchant": frozenset({"merchant"}),
            "auto": frozenset({"auto"}),
            "hybrid": frozenset({"merchant", "auto"}),
        }
        if not self.sources.issubset(allowed[self.mode]):
            raise ValueError("enrollment item sources are incompatible with enrollment mode")
        return self

    def merge_source(
        self,
        source: EnrollmentSource,
        *,
        updated_at: datetime,
    ) -> EnrollmentItem:
        if source in self.sources:
            return self
        allowed: dict[str, frozenset[str]] = {
            "merchant": frozenset({"merchant"}),
            "auto": frozenset({"auto"}),
            "hybrid": frozenset({"merchant", "auto"}),
        }
        if source not in allowed[self.mode]:
            raise ValueError("enrollment source is incompatible with enrollment mode")
        return self._next_version(updated_at=updated_at, sources=self.sources | {source})


class EnrollmentCouponLink(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "enrollment_item_id",
        "coupon_batch_id",
        "benefit_tier",
    )

    enrollment_coupon_link_id: str = Field(min_length=1)
    enrollment_item_id: str = Field(min_length=1)
    coupon_batch_id: str = Field(min_length=1)
    benefit_tier: Literal["base", "boosted"]
    status: Literal["pending", "active", "invalid"]


class ConfirmationTask(BusinessEntity):
    unique_key_fields = ("tenant_id", "enrollment_item_id", "sequence")

    confirmation_task_id: str = Field(min_length=1)
    enrollment_item_id: str = Field(min_length=1)
    subject_type: Literal["merchant", "sales", "sales_manager"]
    subject_id: str = Field(min_length=1, repr=False)
    sequence: int = Field(ge=1)
    due_at: datetime
    timeout_action: Literal["reject", "escalate", "explicit_auto_confirm"]
    status: Literal["pending", "confirmed", "rejected", "timed_out"]

    @field_validator("due_at")
    @classmethod
    def require_aware_due_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "due_at")

    @model_validator(mode="after")
    def validate_due_at(self) -> Self:
        if self.due_at < self.created_at:
            raise ValueError("due_at must not precede created_at")
        return self


class AssortmentSubmission(BusinessEntity):
    unique_key_fields = ("tenant_id", "campaign_id", "submission_version")

    assortment_submission_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    submission_version: str = Field(min_length=1)
    assortment_policy_ref: str = Field(min_length=1)
    assortment_policy_version: str = Field(min_length=1)
    status: Literal["pending", "submitted", "completed", "failed", "unknown"]


class SelectionDecision(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "campaign_id",
        "selection_version",
        "enrollment_item_id",
    )

    selection_decision_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    submission_version: str = Field(min_length=1)
    selection_version: str = Field(min_length=1)
    enrollment_item_id: str = Field(min_length=1)
    decision: Literal["selected", "rejected"]
    reason_code: str | None = None

    @model_validator(mode="after")
    def require_rejection_reason(self) -> Self:
        if self.decision == "rejected" and not self.reason_code:
            raise ValueError("rejected selection decision requires a reason code")
        return self


class ConsumerPlacement(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "campaign_id",
        "selection_version",
        "placement_spec_hash",
    )

    consumer_placement_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    selection_version: str = Field(min_length=1)
    placement_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["pending", "published", "failed", "unknown"]
    request_id: str | None = None
    receipt_id: str | None = None


class MerchantNotification(BusinessEntity):
    unique_key_fields = (
        "tenant_id",
        "merchant_id",
        "campaign_id",
        "result_version",
        "template_id",
        "channel",
    )

    merchant_notification_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    result_version: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    status: Literal["pending", "sent", "retrying", "dead_letter"]
    attempt_count: int = Field(ge=0)
    receipt_id: str | None = None
