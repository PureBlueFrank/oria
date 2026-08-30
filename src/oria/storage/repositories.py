"""SQLite implementations of domain Repository Protocols."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


class MerchantRepositoryError(RuntimeError):
    """Safe repository failure that does not expose SQL or restricted values."""


class BusinessRepositoryError(RuntimeError):
    """Safe business repository failure without SQL or tenant data."""


class SQLiteMerchantRepository:
    """Read and seed tenant-qualified synthetic merchant facts."""

    __slots__ = ("__sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.__sessions = sessions

    async def list_for_eligibility(self, ctx: object) -> tuple[MerchantRecord, ...]:
        tenant_id = getattr(ctx, "tenant_id", None)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise MerchantRepositoryError("trusted tenant context is required")
        try:
            async with self.__sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id, merchant_id, version, display_name, categories_json, "
                        "cities_json, enrollment_systems_json, sales_org_code, active "
                        "FROM merchants WHERE tenant_id = :tenant_id ORDER BY merchant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                rows = result.mappings().all()
            return tuple(
                MerchantRecord(
                    tenant_id=str(row["tenant_id"]),
                    merchant_id=str(row["merchant_id"]),
                    version=int(row["version"]),
                    display_name=str(row["display_name"]),
                    categories=tuple(json.loads(str(row["categories_json"]))),
                    cities=tuple(json.loads(str(row["cities_json"]))),
                    enrollment_systems=tuple(json.loads(str(row["enrollment_systems_json"]))),
                    sales_org_code=str(row["sales_org_code"]),
                    active=bool(row["active"]),
                )
                for row in rows
            )
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MerchantRepositoryError("merchant repository read failed") from exc

    async def seed(self, seed_set: MerchantSeedSet) -> int:
        now = datetime.now(UTC)
        inserted = 0
        try:
            async with self.__sessions.begin() as session:
                existing_result = await session.execute(
                    text(
                        "SELECT merchant_id, version, display_name, categories_json, cities_json, "
                        "enrollment_systems_json, sales_org_code, active FROM merchants "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": seed_set.tenant_id},
                )
                existing = {str(row["merchant_id"]): row for row in existing_result.mappings()}
                for merchant in seed_set.merchants:
                    canonical = {
                        "version": merchant.version,
                        "display_name": merchant.display_name,
                        "categories_json": _canonical_json(merchant.categories),
                        "cities_json": _canonical_json(merchant.cities),
                        "enrollment_systems_json": _canonical_json(merchant.enrollment_systems),
                        "sales_org_code": merchant.internal_sales_org_code(),
                        "active": merchant.active,
                    }
                    row = existing.get(merchant.merchant_id)
                    if row is not None:
                        observed = {name: row[name] for name in canonical}
                        observed["active"] = bool(observed["active"])
                        if observed != canonical:
                            raise MerchantRepositoryError(
                                "existing demo merchant data conflicts with the installed version"
                            )
                        continue
                    await session.execute(
                        text(
                            "INSERT INTO merchants "
                            "(tenant_id, merchant_id, version, display_name, categories_json, "
                            "cities_json, enrollment_systems_json, sales_org_code, active, "
                            "created_at, updated_at) VALUES "
                            "(:tenant_id, :merchant_id, :version, :display_name, :categories_json, "
                            ":cities_json, :enrollment_systems_json, :sales_org_code, :active, "
                            ":created_at, :updated_at)"
                        ),
                        {
                            "tenant_id": seed_set.tenant_id,
                            "merchant_id": merchant.merchant_id,
                            **canonical,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    inserted += 1
        except MerchantRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise MerchantRepositoryError("merchant repository seed failed") from exc
        return inserted


def _canonical_json(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


BusinessEntityT = TypeVar("BusinessEntityT", bound=BusinessEntity)


@dataclass(frozen=True, slots=True)
class _RepositorySpec:
    table: str
    entity_id: str
    model: type[BusinessEntity]
    fields: tuple[tuple[str, str], ...]
    unique_fields: tuple[str, ...]
    json_fields: frozenset[str] = frozenset()
    protected_status: bool = False

    @property
    def columns(self) -> tuple[str, ...]:
        return (
            "tenant_id",
            self.entity_id,
            "version",
            "created_at",
            "updated_at",
            *(column for column, _ in self.fields),
        )


class SQLiteBusinessRepository(Generic[BusinessEntityT]):
    """Tenant-scoped SQLite persistence for one fixed business entity schema."""

    __slots__ = ("__sessions", "__spec")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        spec: _RepositorySpec,
    ) -> None:
        self.__sessions = sessions
        self.__spec = spec

    def _tenant_id(self, ctx: Context) -> str:
        tenant_id = getattr(ctx, "tenant_id", None)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise BusinessRepositoryError("trusted tenant context is required")
        return tenant_id

    def _params(self, entity: BusinessEntityT) -> dict[str, Any]:
        payload = entity.model_dump(mode="json")
        params: dict[str, Any] = {
            "tenant_id": entity.tenant_id,
            self.__spec.entity_id: payload[self.__spec.entity_id],
            "version": entity.version,
            "created_at": payload["created_at"],
            "updated_at": payload["updated_at"],
        }
        for column, attribute in self.__spec.fields:
            value = payload[attribute]
            if column in self.__spec.json_fields:
                if attribute == "sources":
                    value = sorted(value)
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            params[column] = value
        return params

    def _from_row(self, row: Any) -> BusinessEntityT:
        payload: dict[str, Any] = {
            "tenant_id": row["tenant_id"],
            self.__spec.entity_id: row[self.__spec.entity_id],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for column, attribute in self.__spec.fields:
            value = row[column]
            if column in self.__spec.json_fields:
                value = json.loads(str(value))
            payload[attribute] = value
        return cast(BusinessEntityT, self.__spec.model.model_validate(payload))

    def _select_sql(self, where: str) -> str:
        columns = ", ".join(self.__spec.columns)
        return f"SELECT {columns} FROM {self.__spec.table} WHERE {where}"

    async def _find_by_id(
        self,
        session: AsyncSession,
        entity_id: str,
        tenant_id: str,
    ) -> BusinessEntityT | None:
        result = await session.execute(
            text(
                self._select_sql(f"tenant_id = :tenant_id AND {self.__spec.entity_id} = :entity_id")
            ),
            {"tenant_id": tenant_id, "entity_id": entity_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else self._from_row(row)

    def _unique_params(self, unique_key: BusinessKey, tenant_id: str) -> dict[str, str]:
        if len(unique_key) != len(self.__spec.unique_fields) or unique_key[0] != tenant_id:
            raise BusinessRepositoryError("tenant-qualified unique key is invalid")
        return {f"unique_{index}": value for index, value in enumerate(unique_key[1:], start=1)} | {
            "tenant_id": tenant_id
        }

    async def _find_by_unique_key(
        self,
        session: AsyncSession,
        unique_key: BusinessKey,
        tenant_id: str,
    ) -> BusinessEntityT | None:
        params = self._unique_params(unique_key, tenant_id)
        conditions = ["tenant_id = :tenant_id"]
        conditions.extend(
            f"{field} = :unique_{index}"
            for index, field in enumerate(self.__spec.unique_fields[1:], start=1)
        )
        result = await session.execute(
            text(self._select_sql(" AND ".join(conditions))),
            params,
        )
        row = result.mappings().one_or_none()
        return None if row is None else self._from_row(row)

    async def _insert(self, session: AsyncSession, entity: BusinessEntityT) -> None:
        columns = ", ".join(self.__spec.columns)
        values = ", ".join(f":{column}" for column in self.__spec.columns)
        await session.execute(
            text(f"INSERT INTO {self.__spec.table} ({columns}) VALUES ({values})"),
            self._params(entity),
        )

    async def _update(
        self,
        session: AsyncSession,
        existing: BusinessEntityT,
        entity: BusinessEntityT,
        *,
        allow_status_change: bool,
    ) -> None:
        if getattr(existing, self.__spec.entity_id) != getattr(entity, self.__spec.entity_id):
            raise BusinessRepositoryError("unique key conflicts with an existing business identity")
        if existing.created_at != entity.created_at or entity.version != existing.version + 1:
            raise BusinessRepositoryError("business entity optimistic lock conflict")
        if (
            self.__spec.protected_status
            and not allow_status_change
            and existing.model_dump()["status"] != entity.model_dump()["status"]
        ):
            raise BusinessRepositoryError(
                "state changes require the repository transition operation"
            )
        writable = ("version", "updated_at", *(column for column, _ in self.__spec.fields))
        assignments = ", ".join(f"{column} = :{column}" for column in writable)
        params = self._params(entity)
        params["expected_version"] = existing.version
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    f"UPDATE {self.__spec.table} SET {assignments} "
                    f"WHERE tenant_id = :tenant_id AND {self.__spec.entity_id} = "
                    f":{self.__spec.entity_id} AND version = :expected_version"
                ),
                params,
            ),
        )
        if result.rowcount != 1:
            raise BusinessRepositoryError("business entity optimistic lock conflict")

    async def create(self, entity: BusinessEntityT, ctx: Context) -> BusinessEntityT:
        tenant_id = self._tenant_id(ctx)
        if entity.tenant_id != tenant_id:
            raise BusinessRepositoryError("cross-tenant business write is forbidden")
        try:
            async with self.__sessions.begin() as session:
                await self._insert(session, entity)
        except SQLAlchemyError as exc:
            raise BusinessRepositoryError("business entity create failed") from exc
        return entity

    async def get(self, entity_id: str, ctx: Context) -> BusinessEntityT | None:
        tenant_id = self._tenant_id(ctx)
        if not entity_id:
            raise BusinessRepositoryError("business identity is required")
        try:
            async with self.__sessions() as session:
                return await self._find_by_id(session, entity_id, tenant_id)
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessRepositoryError("business entity read failed") from exc

    async def get_by_unique_key(
        self,
        unique_key: BusinessKey,
        ctx: Context,
    ) -> BusinessEntityT | None:
        tenant_id = self._tenant_id(ctx)
        try:
            async with self.__sessions() as session:
                return await self._find_by_unique_key(session, unique_key, tenant_id)
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessRepositoryError("business entity read failed") from exc

    async def upsert_by_unique_key(
        self,
        entity: BusinessEntityT,
        ctx: Context,
    ) -> BusinessEntityT:
        tenant_id = self._tenant_id(ctx)
        if entity.tenant_id != tenant_id:
            raise BusinessRepositoryError("cross-tenant business write is forbidden")
        try:
            async with self.__sessions.begin() as session:
                existing = await self._find_by_unique_key(session, entity.unique_key(), tenant_id)
                if existing is None:
                    await self._insert(session, entity)
                elif existing != entity:
                    await self._update(session, existing, entity, allow_status_change=False)
                else:
                    return existing
        except BusinessRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise BusinessRepositoryError("business entity upsert failed") from exc
        return entity

    async def _transition(
        self,
        entity_id: str,
        target: CampaignStatus | CouponBatchStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> BusinessEntityT:
        tenant_id = self._tenant_id(ctx)
        try:
            async with self.__sessions.begin() as session:
                existing = await self._find_by_id(session, entity_id, tenant_id)
                if existing is None:
                    raise BusinessRepositoryError("business entity is unavailable")
                if isinstance(existing, Campaign):
                    typed = cast(
                        BusinessEntityT,
                        existing.transition_to(cast(CampaignStatus, target), updated_at=updated_at),
                    )
                elif isinstance(existing, CouponBatch):
                    typed = cast(
                        BusinessEntityT,
                        existing.transition_to(
                            cast(CouponBatchStatus, target), updated_at=updated_at
                        ),
                    )
                else:
                    raise BusinessRepositoryError("business entity has no state machine")
                await self._update(session, existing, typed, allow_status_change=True)
                return typed
        except BusinessRepositoryError:
            raise
        except ValueError:
            raise
        except SQLAlchemyError as exc:
            raise BusinessRepositoryError("business entity transition failed") from exc


_PRODUCT_SNAPSHOT = _RepositorySpec(
    "product_snapshots",
    "product_snapshot_id",
    ProductSnapshot,
    (
        ("product_ref", "product_ref"),
        ("product_version", "product_version"),
        ("catalog_snapshot_id", "catalog_snapshot_id"),
        ("attributes_json", "attributes"),
    ),
    ProductSnapshot.unique_key_fields,
    frozenset({"attributes_json"}),
)
_RULE_SNAPSHOT_REF = _RepositorySpec(
    "campaign_rule_snapshot_refs",
    "campaign_rule_snapshot_ref_id",
    CampaignRuleSnapshotRef,
    (("snapshot_id", "snapshot_id"), ("snapshot_hash", "snapshot_hash")),
    CampaignRuleSnapshotRef.unique_key_fields,
)
_CAMPAIGN = _RepositorySpec(
    "campaigns",
    "campaign_id",
    Campaign,
    (
        ("rule_snapshot_ref_id", "rule_snapshot_ref_id"),
        ("enrollment_mode", "enrollment_mode"),
        ("status", "status"),
    ),
    Campaign.unique_key_fields,
    protected_status=True,
)
_COUPON_BATCH = _RepositorySpec(
    "coupon_batches",
    "coupon_batch_id",
    CouponBatch,
    (
        ("campaign_id", "campaign_id"),
        ("coupon_spec_hash", "coupon_spec_hash"),
        ("status", "status"),
    ),
    CouponBatch.unique_key_fields,
    protected_status=True,
)
_LAUNCH_SAGA = _RepositorySpec(
    "launch_saga_states",
    "launch_saga_id",
    LaunchSagaState,
    (("campaign_id", "campaign_id"), ("status", "status"), ("checkpoint", "checkpoint")),
    LaunchSagaState.unique_key_fields,
)
_RECRUITMENT_PUBLICATION = _RepositorySpec(
    "recruitment_publications",
    "recruitment_publication_id",
    RecruitmentPublication,
    (
        ("campaign_id", "campaign_id"),
        ("merchant_scope_hash", "merchant_scope_hash"),
        ("material_version", "material_version"),
        ("status", "status"),
        ("request_id", "request_id"),
        ("receipt_id", "receipt_id"),
    ),
    RecruitmentPublication.unique_key_fields,
)
_ENROLLMENT = _RepositorySpec(
    "enrollments",
    "enrollment_id",
    Enrollment,
    (
        ("campaign_id", "campaign_id"),
        ("merchant_id", "merchant_id"),
        ("mode", "mode"),
        ("status", "status"),
    ),
    Enrollment.unique_key_fields,
)
_ENROLLMENT_ITEM = _RepositorySpec(
    "enrollment_items",
    "enrollment_item_id",
    EnrollmentItem,
    (
        ("enrollment_id", "enrollment_id"),
        ("campaign_id", "campaign_id"),
        ("merchant_id", "merchant_id"),
        ("product_ref", "product_ref"),
        ("product_version", "product_version"),
        ("product_snapshot_id", "product_snapshot_id"),
        ("mode", "mode"),
        ("sources_json", "sources"),
        ("status", "status"),
    ),
    EnrollmentItem.unique_key_fields,
    frozenset({"sources_json"}),
)
_ENROLLMENT_COUPON_LINK = _RepositorySpec(
    "enrollment_coupon_links",
    "enrollment_coupon_link_id",
    EnrollmentCouponLink,
    (
        ("enrollment_item_id", "enrollment_item_id"),
        ("coupon_batch_id", "coupon_batch_id"),
        ("benefit_tier", "benefit_tier"),
        ("status", "status"),
    ),
    EnrollmentCouponLink.unique_key_fields,
)
_CONFIRMATION_TASK = _RepositorySpec(
    "confirmation_tasks",
    "confirmation_task_id",
    ConfirmationTask,
    (
        ("enrollment_item_id", "enrollment_item_id"),
        ("subject_type", "subject_type"),
        ("subject_id", "subject_id"),
        ("sequence", "sequence"),
        ("due_at", "due_at"),
        ("timeout_action", "timeout_action"),
        ("status", "status"),
    ),
    ConfirmationTask.unique_key_fields,
)
_ASSORTMENT_SUBMISSION = _RepositorySpec(
    "assortment_submissions",
    "assortment_submission_id",
    AssortmentSubmission,
    (
        ("campaign_id", "campaign_id"),
        ("submission_version", "submission_version"),
        ("assortment_policy_ref", "assortment_policy_ref"),
        ("assortment_policy_version", "assortment_policy_version"),
        ("status", "status"),
    ),
    AssortmentSubmission.unique_key_fields,
)
_SELECTION_DECISION = _RepositorySpec(
    "selection_decisions",
    "selection_decision_id",
    SelectionDecision,
    (
        ("campaign_id", "campaign_id"),
        ("submission_version", "submission_version"),
        ("selection_version", "selection_version"),
        ("enrollment_item_id", "enrollment_item_id"),
        ("decision", "decision"),
        ("reason_code", "reason_code"),
    ),
    SelectionDecision.unique_key_fields,
)
_CONSUMER_PLACEMENT = _RepositorySpec(
    "consumer_placements",
    "consumer_placement_id",
    ConsumerPlacement,
    (
        ("campaign_id", "campaign_id"),
        ("selection_version", "selection_version"),
        ("placement_spec_hash", "placement_spec_hash"),
        ("status", "status"),
        ("request_id", "request_id"),
        ("receipt_id", "receipt_id"),
    ),
    ConsumerPlacement.unique_key_fields,
)
_MERCHANT_NOTIFICATION = _RepositorySpec(
    "merchant_notifications",
    "merchant_notification_id",
    MerchantNotification,
    (
        ("merchant_id", "merchant_id"),
        ("campaign_id", "campaign_id"),
        ("result_version", "result_version"),
        ("template_id", "template_id"),
        ("channel", "channel"),
        ("status", "status"),
        ("attempt_count", "attempt_count"),
        ("receipt_id", "receipt_id"),
    ),
    MerchantNotification.unique_key_fields,
)


class SQLiteProductSnapshotRepository(SQLiteBusinessRepository[ProductSnapshot]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _PRODUCT_SNAPSHOT)


class SQLiteCampaignRuleSnapshotRefRepository(SQLiteBusinessRepository[CampaignRuleSnapshotRef]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _RULE_SNAPSHOT_REF)


class SQLiteCampaignRepository(SQLiteBusinessRepository[Campaign]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _CAMPAIGN)

    async def transition(
        self,
        campaign_id: str,
        target: CampaignStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> Campaign:
        return await self._transition(campaign_id, target, updated_at, ctx)


class SQLiteCouponBatchRepository(SQLiteBusinessRepository[CouponBatch]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _COUPON_BATCH)

    async def transition(
        self,
        coupon_batch_id: str,
        target: CouponBatchStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> CouponBatch:
        return await self._transition(coupon_batch_id, target, updated_at, ctx)


class SQLiteLaunchSagaStateRepository(SQLiteBusinessRepository[LaunchSagaState]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _LAUNCH_SAGA)


class SQLiteRecruitmentPublicationRepository(SQLiteBusinessRepository[RecruitmentPublication]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _RECRUITMENT_PUBLICATION)


class SQLiteEnrollmentRepository(SQLiteBusinessRepository[Enrollment]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _ENROLLMENT)


class SQLiteEnrollmentItemRepository(SQLiteBusinessRepository[EnrollmentItem]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _ENROLLMENT_ITEM)


class SQLiteEnrollmentCouponLinkRepository(SQLiteBusinessRepository[EnrollmentCouponLink]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _ENROLLMENT_COUPON_LINK)


class SQLiteConfirmationTaskRepository(SQLiteBusinessRepository[ConfirmationTask]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _CONFIRMATION_TASK)


class SQLiteAssortmentSubmissionRepository(SQLiteBusinessRepository[AssortmentSubmission]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _ASSORTMENT_SUBMISSION)


class SQLiteSelectionDecisionRepository(SQLiteBusinessRepository[SelectionDecision]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _SELECTION_DECISION)


class SQLiteConsumerPlacementRepository(SQLiteBusinessRepository[ConsumerPlacement]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _CONSUMER_PLACEMENT)


class SQLiteMerchantNotificationRepository(SQLiteBusinessRepository[MerchantNotification]):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions, _MERCHANT_NOTIFICATION)
