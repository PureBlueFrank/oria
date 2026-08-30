"""Repository Protocols owned by the domain layer."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

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
    MerchantNotification,
    ProductSnapshot,
    RecruitmentPublication,
    SelectionDecision,
)
from oria.domain.models import MerchantRecord, MerchantSeedSet

if TYPE_CHECKING:
    from oria.core.context import Context


class MerchantRepository(Protocol):
    """Tenant-scoped merchant facts; no arbitrary predicates cross this seam."""

    async def list_for_eligibility(self, ctx: Context) -> tuple[MerchantRecord, ...]: ...

    async def seed(self, seed_set: MerchantSeedSet) -> int: ...


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
    pass


class RecruitmentPublicationRepository(BusinessEntityRepository[RecruitmentPublication], Protocol):
    pass


class EnrollmentRepository(BusinessEntityRepository[Enrollment], Protocol):
    pass


class EnrollmentItemRepository(BusinessEntityRepository[EnrollmentItem], Protocol):
    pass


class EnrollmentCouponLinkRepository(BusinessEntityRepository[EnrollmentCouponLink], Protocol):
    pass


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
