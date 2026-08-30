"""SQLite Repository integration coverage for all V0.3-T01 business entities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oria.config import resolve_runtime_config
from oria.data import initialize_data
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
from oria.domain.repositories import CampaignRepository, CouponBatchRepository
from oria.storage.database import DatabaseResources
from oria.storage.repositories import (
    BusinessRepositoryError,
    SQLiteAssortmentSubmissionRepository,
    SQLiteCampaignRepository,
    SQLiteCampaignRuleSnapshotRefRepository,
    SQLiteConfirmationTaskRepository,
    SQLiteConsumerPlacementRepository,
    SQLiteCouponBatchRepository,
    SQLiteEnrollmentCouponLinkRepository,
    SQLiteEnrollmentItemRepository,
    SQLiteEnrollmentRepository,
    SQLiteLaunchSagaStateRepository,
    SQLiteMerchantNotificationRepository,
    SQLiteProductSnapshotRepository,
    SQLiteRecruitmentPublicationRepository,
    SQLiteSelectionDecisionRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _common() -> dict[str, object]:
    return {
        "tenant_id": "local-community",
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _business_graph() -> tuple[object, ...]:
    common = _common()
    return (
        ProductSnapshot(
            **common,
            product_snapshot_id="product-snapshot-1",
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
            merchant_id="demo-m001",
            mode="hybrid",
            status="open",
        ),
        EnrollmentItem(
            **common,
            enrollment_item_id="item-1",
            enrollment_id="enrollment-1",
            campaign_id="campaign-1",
            merchant_id="demo-m001",
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
            subject_id="demo-m001",
            sequence=1,
            due_at=NOW + timedelta(days=1),
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
            merchant_id="demo-m001",
            campaign_id="campaign-1",
            result_version="v1",
            template_id="template-1",
            channel="mock-im",
            status="pending",
            attempt_count=0,
        ),
    )


@pytest.mark.asyncio
async def test_all_repositories_round_trip_unique_upsert_state_and_tenant_guards(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    ctx: Any = SimpleNamespace(tenant_id="local-community")
    other_ctx: Any = SimpleNamespace(tenant_id="other-tenant")
    entities = _business_graph()

    async with DatabaseResources(config) as databases:
        sessions = databases.business_sessions
        repositories = (
            SQLiteProductSnapshotRepository(sessions),
            SQLiteCampaignRuleSnapshotRefRepository(sessions),
            SQLiteCampaignRepository(sessions),
            SQLiteCouponBatchRepository(sessions),
            SQLiteLaunchSagaStateRepository(sessions),
            SQLiteRecruitmentPublicationRepository(sessions),
            SQLiteEnrollmentRepository(sessions),
            SQLiteEnrollmentItemRepository(sessions),
            SQLiteEnrollmentCouponLinkRepository(sessions),
            SQLiteConfirmationTaskRepository(sessions),
            SQLiteAssortmentSubmissionRepository(sessions),
            SQLiteSelectionDecisionRepository(sessions),
            SQLiteConsumerPlacementRepository(sessions),
            SQLiteMerchantNotificationRepository(sessions),
        )
        entity_ids = (
            "product-snapshot-1",
            "rule-ref-1",
            "campaign-1",
            "coupon-1",
            "saga-1",
            "publication-1",
            "enrollment-1",
            "item-1",
            "link-1",
            "confirmation-1",
            "submission-1",
            "decision-1",
            "placement-1",
            "notification-1",
        )
        campaign_contract: CampaignRepository = repositories[2]
        coupon_contract: CouponBatchRepository = repositories[3]

        for repository, entity, entity_id in zip(repositories, entities, entity_ids, strict=True):
            assert await repository.create(entity, ctx) == entity
            assert await repository.get(entity_id, ctx) == entity
            assert await repository.get_by_unique_key(entity.unique_key(), ctx) == entity
            assert await repository.upsert_by_unique_key(entity, ctx) == entity

        product = entities[0]
        assert isinstance(product, ProductSnapshot)
        updated_product = product.model_copy(
            update={
                "version": 2,
                "updated_at": NOW + timedelta(minutes=1),
                "attributes": {"category": "synthetic", "price": 10},
            }
        )
        assert await repositories[0].upsert_by_unique_key(updated_product, ctx) == updated_product
        assert await repositories[0].get("product-snapshot-1", ctx) == updated_product

        item = entities[7]
        assert isinstance(item, EnrollmentItem)
        merged_item = item.merge_source("merchant", updated_at=NOW + timedelta(minutes=1))
        assert await repositories[7].upsert_by_unique_key(merged_item, ctx) == merged_item
        assert (await repositories[7].get("item-1", ctx)).sources == frozenset(  # type: ignore[union-attr]
            {"auto", "merchant"}
        )

        campaign = entities[2]
        assert isinstance(campaign, Campaign)
        naked_status_change = campaign.model_copy(
            update={
                "version": 2,
                "updated_at": NOW + timedelta(minutes=1),
                "status": "pending_launch_approval",
            }
        )
        with pytest.raises(BusinessRepositoryError, match="transition"):
            await repositories[2].upsert_by_unique_key(naked_status_change, ctx)
        transitioned_campaign = await campaign_contract.transition(
            "campaign-1", "pending_launch_approval", NOW + timedelta(minutes=1), ctx
        )
        assert transitioned_campaign.version == 2
        with pytest.raises(ValueError, match="illegal state transition"):
            await campaign_contract.transition(
                "campaign-1", "selecting", NOW + timedelta(minutes=2), ctx
            )

        transitioned_coupon = await coupon_contract.transition(
            "coupon-1", "materializing", NOW + timedelta(minutes=1), ctx
        )
        assert transitioned_coupon.status == "materializing"

        assert await repositories[2].get("campaign-1", other_ctx) is None
        with pytest.raises(BusinessRepositoryError, match="tenant-qualified"):
            await repositories[2].get_by_unique_key(campaign.unique_key(), other_ctx)
        with pytest.raises(BusinessRepositoryError, match="cross-tenant"):
            await repositories[13].create(
                entities[13].model_copy(update={"tenant_id": "other-tenant"}), ctx
            )

        stale_product = product.model_copy(update={"attributes": {"category": "stale"}})
        with pytest.raises(BusinessRepositoryError, match="optimistic lock"):
            await repositories[0].upsert_by_unique_key(stale_product, ctx)
