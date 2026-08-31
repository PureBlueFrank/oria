"""Repository Protocols owned by the domain layer."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from oria.core.approvals import ApprovalBusinessBinding
from oria.domain.business import (
    AssortmentSubmission,
    BusinessEntity,
    BusinessKey,
    Campaign,
    CampaignRuleSnapshotRef,
    CampaignStatus,
    ConfirmationTask,
    ConsumerPlacement,
    CouponBatch,
    CouponBatchStatus,
    Enrollment,
    EnrollmentCouponLink,
    EnrollmentItem,
    LaunchSagaState,
    LaunchSagaStatus,
    MerchantNotification,
    ProductSnapshot,
    RecruitmentPublication,
    SelectionDecision,
)
from oria.domain.models import EligibilityCriteria, MerchantRecord, MerchantSeedSet
from oria.domain.product_eligibility import (
    EnrollmentEligibilityAttestation,
    ProductEligibilityCriteria,
    ProductSellabilityAttestation,
)
from oria.domain.product_eligibility import (
    ProductSnapshot as CatalogProductSnapshot,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oria.core.context import Context


class MerchantRepository(Protocol):
    """Tenant-scoped merchant facts; no arbitrary predicates cross this seam."""

    async def list_for_eligibility(self, ctx: Context) -> tuple[MerchantRecord, ...]: ...

    async def seed(self, seed_set: MerchantSeedSet) -> int: ...


class CampaignDraftRepository(Protocol):
    """Atomic persistence boundary for a validated local campaign draft."""

    async def create_bundle(
        self,
        *,
        rule_snapshot_ref: CampaignRuleSnapshotRef,
        campaign: Campaign,
        coupon_batch: CouponBatch,
        recruitment_publication: RecruitmentPublication,
        ctx: Context,
    ) -> None: ...


EntityT = TypeVar("EntityT", bound=BusinessEntity)


class BusinessEntityRepository(Protocol, Generic[EntityT]):
    """Fixed tenant-scoped operations; arbitrary predicates never cross this seam."""

    async def create(self, entity: EntityT, ctx: Context) -> EntityT: ...

    async def get(self, entity_id: str, ctx: Context) -> EntityT | None: ...

    async def get_by_unique_key(
        self,
        unique_key: BusinessKey,
        ctx: Context,
    ) -> EntityT | None: ...

    async def upsert_by_unique_key(self, entity: EntityT, ctx: Context) -> EntityT: ...


class ProductSnapshotRepository(BusinessEntityRepository[ProductSnapshot], Protocol):
    pass


class CampaignRuleSnapshotRefRepository(
    BusinessEntityRepository[CampaignRuleSnapshotRef], Protocol
):
    pass


class CampaignRepository(BusinessEntityRepository[Campaign], Protocol):
    async def transition(
        self,
        campaign_id: str,
        target: CampaignStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> Campaign: ...


class CouponBatchRepository(BusinessEntityRepository[CouponBatch], Protocol):
    async def transition(
        self,
        coupon_batch_id: str,
        target: CouponBatchStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> CouponBatch: ...


class LaunchSagaStateRepository(BusinessEntityRepository[LaunchSagaState], Protocol):
    async def transition(
        self,
        launch_saga_id: str,
        target: LaunchSagaStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> LaunchSagaState: ...


class RecruitmentPublicationRepository(BusinessEntityRepository[RecruitmentPublication], Protocol):
    pass


class EnrollmentRepository(BusinessEntityRepository[Enrollment], Protocol):
    pass


class EnrollmentItemRepository(BusinessEntityRepository[EnrollmentItem], Protocol):
    pass


class EnrollmentCouponLinkRepository(BusinessEntityRepository[EnrollmentCouponLink], Protocol):
    pass


class EnrollmentWorkflowRepository(Protocol):
    async def get_approval_binding(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
    ) -> ApprovalBusinessBinding | None: ...

    async def upsert_enrollment_items(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        source: str,
        bundles: tuple[
            tuple[ProductSnapshot, Enrollment, EnrollmentItem, tuple[ConfirmationTask, ...]], ...
        ],
        new_enrollment_version: bool,
        expected_approval_binding: ApprovalBusinessBinding | None,
        updated_approval_binding: ApprovalBusinessBinding,
        merchant_criteria: EligibilityCriteria,
        product_criteria: ProductEligibilityCriteria,
        eligibility_attestation: EnrollmentEligibilityAttestation,
    ) -> None: ...

    async def load_enrollment_items(
        self,
        *,
        tenant_id: str,
        enrollment_item_ids: tuple[str, ...],
    ) -> tuple[tuple[EnrollmentItem, ...], tuple[ConfirmationTask, ...]]: ...

    async def link_coupon_batch(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        coupon_batch_id: str,
        coupon_batch_version: int,
        rule_snapshot_ref_id: str,
        allowed_tiers: frozenset[str],
        merchant_criteria: EligibilityCriteria,
        product_criteria: ProductEligibilityCriteria,
        rule_snapshot_hash: str,
        expected_approval_binding: ApprovalBusinessBinding,
        updated_approval_binding: ApprovalBusinessBinding,
        current_products: tuple[CatalogProductSnapshot, ...],
        sellability_attestation: ProductSellabilityAttestation,
        links: tuple[EnrollmentCouponLink, ...],
    ) -> None: ...

    async def load_coupon_links(
        self,
        *,
        tenant_id: str,
        link_ids: tuple[str, ...],
    ) -> tuple[EnrollmentCouponLink, ...]: ...

    async def load_confirmation_chain(
        self,
        *,
        tenant_id: str,
        confirmation_task_id: str,
    ) -> tuple[EnrollmentItem, tuple[ConfirmationTask, ...]]: ...

    async def apply_confirmation_chain(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        expected_item: EnrollmentItem,
        expected_tasks: tuple[ConfirmationTask, ...],
        updated_item: EnrollmentItem,
        updated_tasks: tuple[ConfirmationTask, ...],
    ) -> None: ...


class ConfirmationTaskRepository(BusinessEntityRepository[ConfirmationTask], Protocol):
    pass


class AssortmentSubmissionRepository(BusinessEntityRepository[AssortmentSubmission], Protocol):
    pass


class SelectionDecisionRepository(BusinessEntityRepository[SelectionDecision], Protocol):
    pass


class ConsumerPlacementRepository(BusinessEntityRepository[ConsumerPlacement], Protocol):
    pass


class MerchantNotificationRepository(BusinessEntityRepository[MerchantNotification], Protocol):
    pass


class CampaignLaunchRepository(Protocol):
    async def load_draft_entities(
        self,
        *,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        coupon_batch_id: str,
        recruitment_publication_id: str,
        ctx: Context,
    ) -> tuple[Campaign, CampaignRuleSnapshotRef, CouponBatch, RecruitmentPublication]: ...

    async def get_saga(self, campaign_id: str, ctx: Context) -> LaunchSagaState | None: ...

    async def create_saga(self, saga: LaunchSagaState, ctx: Context) -> LaunchSagaState: ...

    async def transition_saga(
        self,
        saga: LaunchSagaState,
        target: LaunchSagaStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> LaunchSagaState: ...

    async def mark_coupon_ready(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        coupon_batch_id: str,
        updated_at: datetime,
    ) -> None: ...

    async def mark_recruitment_published(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        recruitment_publication_id: str,
        request_id: str,
        receipt_id: str,
        updated_at: datetime,
    ) -> None: ...
