"""V0.3-T01 domain values, tenant keys, and explicit state-machine tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oria.domain.business import (
    AssortmentSubmission,
    Campaign,
    CampaignRuleSnapshotRef,
    ConfirmationTask,
    ConsumerPlacement,
    CouponBatch,
    Enrollment,
    EnrollmentCouponLink,
    EnrollmentItem,
    LaunchSagaState,
    MerchantNotification,
    ProductSnapshot,
    RecruitmentPublication,
    SelectionDecision,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _common() -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _entities() -> tuple[object, ...]:
    common = _common()
    return (
        ProductSnapshot(
            **common,
            product_snapshot_id="product-snapshot-1",
            merchant_id="merchant-1",
            product_ref="product-1",
            product_version="v1",
            catalog_snapshot_id="catalog-v1",
            attributes={"category": "synthetic"},
        ),
        CampaignRuleSnapshotRef(
            **common,
            campaign_rule_snapshot_ref_id="rule-ref-1",
            snapshot_id="rs_123456789012345678901234",
            snapshot_hash=HASH_A,
        ),
        Campaign(
            **common,
            campaign_id="campaign-1",
            rule_snapshot_ref_id="rule-ref-1",
            enrollment_mode="hybrid",
        ),
        CouponBatch(
            **common,
            coupon_batch_id="coupon-1",
            campaign_id="campaign-1",
            coupon_spec_hash=HASH_A,
        ),
        LaunchSagaState(
            **common,
            launch_saga_id="saga-1",
            campaign_id="campaign-1",
            status="planned",
            checkpoint="draft-persisted",
        ),
        RecruitmentPublication(
            **common,
            recruitment_publication_id="publication-1",
            campaign_id="campaign-1",
            merchant_scope_hash=HASH_B,
            material_version="v1",
            status="pending",
        ),
        Enrollment(
            **common,
            enrollment_id="enrollment-1",
            campaign_id="campaign-1",
            merchant_id="merchant-1",
            mode="hybrid",
            status="open",
        ),
        EnrollmentItem(
            **common,
            enrollment_item_id="item-1",
            enrollment_id="enrollment-1",
            campaign_id="campaign-1",
            merchant_id="merchant-1",
            product_ref="product-1",
            product_version="v1",
            product_snapshot_id="product-snapshot-1",
            mode="hybrid",
            sources=frozenset({"auto"}),
            status="pending_confirmation",
        ),
        EnrollmentCouponLink(
            **common,
            enrollment_coupon_link_id="link-1",
            enrollment_item_id="item-1",
            coupon_batch_id="coupon-1",
            benefit_tier="base",
            status="pending",
        ),
        ConfirmationTask(
            **common,
            confirmation_task_id="confirmation-1",
            enrollment_item_id="item-1",
            subject_type="merchant",
            subject_id="merchant-1",
            sequence=1,
            due_at=LATER,
            timeout_action="reject",
            status="pending",
        ),
        AssortmentSubmission(
            **common,
            assortment_submission_id="submission-1",
            campaign_id="campaign-1",
            submission_version="v1",
            assortment_policy_ref="policy-1",
            assortment_policy_version="v1",
            status="pending",
        ),
        SelectionDecision(
            **common,
            selection_decision_id="decision-1",
            campaign_id="campaign-1",
            submission_version="v1",
            selection_version="selection-v1",
            enrollment_item_id="item-1",
            decision="selected",
        ),
        ConsumerPlacement(
            **common,
            consumer_placement_id="placement-1",
            campaign_id="campaign-1",
            selection_version="selection-v1",
            placement_spec_hash=HASH_A,
            status="pending",
        ),
        MerchantNotification(
            **common,
            merchant_notification_id="notification-1",
            merchant_id="merchant-1",
            campaign_id="campaign-1",
            result_version="v1",
            template_id="template-1",
            channel="mock-im",
            status="pending",
            attempt_count=0,
        ),
    )


@pytest.mark.parametrize("entity", _entities(), ids=lambda item: type(item).__name__)
def test_every_business_entity_is_immutable_and_has_tenant_composite_key(entity: object) -> None:
    key = entity.unique_key()  # type: ignore[attr-defined]

    assert key[0] == "tenant-a"
    assert entity.version == 1  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        entity.version = 2  # type: ignore[attr-defined]


@pytest.mark.parametrize("entity", _entities(), ids=lambda item: type(item).__name__)
def test_every_business_entity_rejects_invalid_common_fields(entity: object) -> None:
    payload = entity.model_dump()  # type: ignore[attr-defined]
    payload["tenant_id"] = ""
    payload["version"] = 0
    payload["updated_at"] = NOW - timedelta(seconds=1)

    with pytest.raises(ValidationError):
        type(entity).model_validate(payload)


def test_campaign_accepts_only_the_declared_eight_state_graph() -> None:
    campaign = _entities()[2]
    assert isinstance(campaign, Campaign)
    legal_path = (
        "pending_launch_approval",
        "recruiting",
        "selecting",
        "pending_consumer_publish",
        "active",
        "completed",
    )
    for index, target in enumerate(legal_path, start=1):
        campaign = campaign.transition_to(target, updated_at=NOW + timedelta(minutes=index))  # type: ignore[arg-type]

    assert campaign.status == "completed"
    assert campaign.version == 7
    with pytest.raises(ValueError, match="illegal state transition"):
        campaign.transition_to("active", updated_at=LATER)

    active = _entities()[2]
    assert isinstance(active, Campaign)
    for index, target in enumerate(legal_path[:-1], start=1):
        active = active.transition_to(target, updated_at=NOW + timedelta(minutes=index))  # type: ignore[arg-type]
    assert active.transition_to("cancelled", updated_at=NOW + timedelta(minutes=7)).status == (
        "cancelled"
    )


@pytest.mark.parametrize(
    "terminal",
    ["ready", "failed", "unknown"],
)
def test_coupon_batch_accepts_every_declared_materialization_outcome(terminal: str) -> None:
    batch = _entities()[3]
    assert isinstance(batch, CouponBatch)
    materializing = batch.transition_to("materializing", updated_at=LATER)
    outcome = materializing.transition_to(terminal, updated_at=LATER + timedelta(minutes=1))  # type: ignore[arg-type]

    assert outcome.transition_to("expired", updated_at=LATER + timedelta(minutes=2)).status == (
        "expired"
    )
    with pytest.raises(ValueError, match="illegal state transition"):
        batch.transition_to("ready", updated_at=LATER)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("planned", "coupon_materialized"),
        ("coupon_materialized", "recruitment_published"),
        ("recruitment_published", "completed"),
        ("planned", "compensation_pending"),
        ("planned", "reconciliation_required"),
        ("planned", "failed"),
        ("coupon_materialized", "compensation_pending"),
        ("coupon_materialized", "reconciliation_required"),
        ("coupon_materialized", "failed"),
        ("recruitment_published", "compensation_pending"),
        ("recruitment_published", "reconciliation_required"),
        ("recruitment_published", "failed"),
    ],
)
def test_launch_saga_accepts_every_declared_transition(source: str, target: str) -> None:
    saga = _entities()[4]
    assert isinstance(saga, LaunchSagaState)
    if source == "coupon_materialized":
        saga = saga.transition_to("coupon_materialized", updated_at=LATER)
    elif source == "recruitment_published":
        saga = saga.transition_to("coupon_materialized", updated_at=LATER).transition_to(
            "recruitment_published",
            updated_at=LATER + timedelta(minutes=1),
        )

    transitioned = saga.transition_to(  # type: ignore[arg-type]
        target,
        updated_at=LATER + timedelta(minutes=2),
    )

    assert transitioned.status == target
    assert transitioned.version == saga.version + 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("planned", "recruitment_published"),
        ("planned", "completed"),
        ("coupon_materialized", "planned"),
        ("coupon_materialized", "completed"),
        ("recruitment_published", "planned"),
        ("recruitment_published", "coupon_materialized"),
        ("completed", "failed"),
        ("compensation_pending", "failed"),
        ("reconciliation_required", "failed"),
        ("failed", "planned"),
    ],
)
def test_launch_saga_rejects_skips_backtracking_and_terminal_transitions(
    source: str,
    target: str,
) -> None:
    saga = _entities()[4]
    assert isinstance(saga, LaunchSagaState)
    path = {
        "coupon_materialized": ("coupon_materialized",),
        "recruitment_published": ("coupon_materialized", "recruitment_published"),
        "completed": ("coupon_materialized", "recruitment_published", "completed"),
        "compensation_pending": ("compensation_pending",),
        "reconciliation_required": ("reconciliation_required",),
        "failed": ("failed",),
    }.get(source, ())
    for index, step in enumerate(path, start=1):
        saga = saga.transition_to(  # type: ignore[arg-type]
            step,
            updated_at=NOW + timedelta(minutes=index),
        )

    with pytest.raises(ValueError, match="illegal state transition"):
        saga.transition_to(  # type: ignore[arg-type]
            target,
            updated_at=NOW + timedelta(minutes=10),
        )


@pytest.mark.parametrize(
    ("mode", "sources"),
    [
        ("merchant", frozenset({"auto"})),
        ("auto", frozenset({"merchant"})),
        ("hybrid", frozenset()),
    ],
)
def test_enrollment_item_sources_must_match_mode(mode: str, sources: frozenset[str]) -> None:
    item = _entities()[7]
    assert isinstance(item, EnrollmentItem)

    with pytest.raises(ValidationError):
        EnrollmentItem.model_validate({**item.model_dump(), "mode": mode, "sources": sources})


def test_hybrid_enrollment_item_merges_dual_sources_idempotently() -> None:
    item = _entities()[7]
    assert isinstance(item, EnrollmentItem)

    merged = item.merge_source("merchant", updated_at=LATER)
    repeated = merged.merge_source("merchant", updated_at=LATER + timedelta(minutes=1))

    assert merged.sources == frozenset({"merchant", "auto"})
    assert merged.version == 2
    assert repeated is merged


def test_cross_tenant_association_is_rejected_before_repository_write() -> None:
    campaign = _entities()[2]
    batch = _entities()[3]
    assert isinstance(campaign, Campaign)
    assert isinstance(batch, CouponBatch)
    other_tenant_batch = batch.model_copy(update={"tenant_id": "tenant-b"})

    campaign.validate_tenant_links(batch)
    with pytest.raises(ValueError, match="cross-tenant"):
        campaign.validate_tenant_links(other_tenant_batch)


def test_product_and_rule_snapshot_references_remain_reproducible() -> None:
    product = _entities()[0]
    rule = _entities()[1]
    assert isinstance(product, ProductSnapshot)
    assert isinstance(rule, CampaignRuleSnapshotRef)

    assert product.unique_key() == ("tenant-a", "merchant-1", "product-1", "v1")
    assert product.catalog_snapshot_id == "catalog-v1"
    assert rule.unique_key() == (
        "tenant-a",
        "rs_123456789012345678901234",
        HASH_A,
    )
    assert not hasattr(rule, "basic")


def test_selection_rejection_requires_a_reason() -> None:
    selected = _entities()[11]
    assert isinstance(selected, SelectionDecision)

    with pytest.raises(ValidationError, match="reason code"):
        SelectionDecision.model_validate(
            {**selected.model_dump(), "decision": "rejected", "reason_code": None}
        )
