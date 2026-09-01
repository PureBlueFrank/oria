"""SQLite implementations of domain Repository Protocols."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.models import EligibilityCriteria, MerchantRecord, MerchantSeedSet
from oria.domain.product_eligibility import (
    EnrollmentEligibilityAttestation,
    ProductEligibilityCriteria,
    ProductEligibilityPolicy,
    ProductSellabilityAttestation,
)
from oria.domain.product_eligibility import ProductSnapshot as CatalogProductSnapshot

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


def _item_keys_hash(items: tuple[EnrollmentItem, ...]) -> str:
    keys = sorted((item.merchant_id, item.product_ref, item.product_version) for item in items)
    payload = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


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
        target: CampaignStatus | CouponBatchStatus | LaunchSagaStatus,
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
                elif isinstance(existing, LaunchSagaState):
                    typed = cast(
                        BusinessEntityT,
                        existing.transition_to(
                            cast(LaunchSagaStatus, target), updated_at=updated_at
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
        ("merchant_id", "merchant_id"),
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
    protected_status=True,
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

    async def transition(
        self,
        launch_saga_id: str,
        target: LaunchSagaStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> LaunchSagaState:
        return await self._transition(launch_saga_id, target, updated_at, ctx)


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


class SQLiteEnrollmentWorkflowRepository:
    """Atomic T05 enrollment aggregation and coupon-link persistence."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._products = SQLiteProductSnapshotRepository(sessions)
        self._campaigns = SQLiteCampaignRepository(sessions)
        self._enrollments = SQLiteEnrollmentRepository(sessions)
        self._items = SQLiteEnrollmentItemRepository(sessions)
        self._tasks = SQLiteConfirmationTaskRepository(sessions)
        self._links = SQLiteEnrollmentCouponLinkRepository(sessions)
        self._coupons = SQLiteCouponBatchRepository(sessions)

    async def get_approval_binding(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
    ) -> ApprovalBusinessBinding | None:
        try:
            async with self._sessions() as session:
                return await self._find_approval_binding(session, tenant_id, campaign_id)
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("approval business binding read failed") from exc

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
    ) -> None:
        campaign = await self._campaigns._find_by_id(session, campaign_id, tenant_id)
        if (
            campaign is None
            or campaign.status != "recruiting"
            or campaign.rule_snapshot_ref_id != rule_snapshot_ref_id
        ):
            raise BusinessRepositoryError("campaign is not accepting enrollment items")
        if source not in {"merchant", "auto"}:
            raise BusinessRepositoryError("enrollment source is invalid")
        current_binding = await self._find_approval_binding(session, tenant_id, campaign_id)
        if current_binding != expected_approval_binding:
            raise BusinessRepositoryError("approval business binding optimistic lock conflict")
        if updated_approval_binding.campaign_id != campaign_id:
            raise BusinessRepositoryError("approval business binding campaign does not match")
        items = tuple(item for _, _, item, _ in bundles)
        eligibility_attestation.verify(
            merchant_criteria=merchant_criteria,
            product_criteria=product_criteria,
            item_business_keys_hash=_item_keys_hash(items),
        )
        if (
            eligibility_attestation.campaign_id != campaign_id
            or eligibility_attestation.rule_snapshot_ref_id != rule_snapshot_ref_id
            or eligibility_attestation.rule_snapshot_hash
            != updated_approval_binding.rule_snapshot_hash
        ):
            raise BusinessRepositoryError("enrollment eligibility rule binding does not match")
        for product, enrollment, item, tasks in bundles:
            if any(entity.tenant_id != tenant_id for entity in (product, enrollment, item, *tasks)):
                raise BusinessRepositoryError("cross-tenant enrollment write is forbidden")
            self._validate_enrollment_bundle(product, enrollment, item, tasks)
            if (
                enrollment.campaign_id != campaign_id
                or item.campaign_id != campaign_id
                or item.mode != campaign.enrollment_mode
                or enrollment.mode != campaign.enrollment_mode
                or source not in item.sources
            ):
                raise BusinessRepositoryError("enrollment bundle does not match its campaign")
            merchant = await self._load_merchant(session, tenant_id, item.merchant_id)
            if not EligibilityPolicy().evaluate(merchant, merchant_criteria).eligible:
                raise BusinessRepositoryError("enrollment merchant no longer satisfies hard policy")

        await self._write_approval_binding(
            session,
            tenant_id=tenant_id,
            current=current_binding,
            updated=updated_approval_binding,
        )

        bumped_enrollments: set[str] = set()
        for product, enrollment, item, tasks in bundles:
            existing_product = await self._products._find_by_unique_key(
                session, product.unique_key(), tenant_id
            )
            persisted_product = product if existing_product is None else existing_product
            self._validate_product_bundle(
                persisted_product,
                item,
                product_criteria=product_criteria,
                attestation=eligibility_attestation,
            )
            if existing_product is None:
                await self._products._insert(session, product)
            elif (
                existing_product.product_snapshot_id != product.product_snapshot_id
                or existing_product.attributes != product.attributes
            ):
                raise BusinessRepositoryError("product snapshot version conflicts with history")

            existing_enrollment = await self._enrollments._find_by_unique_key(
                session, enrollment.unique_key(), tenant_id
            )
            if existing_enrollment is None:
                await self._enrollments._insert(session, enrollment)
            elif existing_enrollment.mode != campaign.enrollment_mode:
                raise BusinessRepositoryError("enrollment mode conflicts with its campaign")
            elif existing_enrollment.status not in {"open", "submitted"}:
                raise BusinessRepositoryError("enrollment is not writable")
            elif (
                new_enrollment_version
                and existing_enrollment.enrollment_id not in bumped_enrollments
            ):
                updated = existing_enrollment._next_version(
                    updated_at=item.updated_at,
                    status="submitted",
                )
                await self._enrollments._update(
                    session, existing_enrollment, updated, allow_status_change=True
                )
                bumped_enrollments.add(existing_enrollment.enrollment_id)

            existing_item = await self._items._find_by_unique_key(
                session, item.unique_key(), tenant_id
            )
            if existing_item is None:
                await self._items._insert(session, item)
                for task in tasks:
                    await self._tasks._insert(session, task)
                continue
            if (
                existing_item.enrollment_id != item.enrollment_id
                or existing_item.product_snapshot_id != item.product_snapshot_id
                or existing_item.mode != item.mode
            ):
                raise BusinessRepositoryError("enrollment item business key conflicts")
            merged = existing_item.merge_source(source, updated_at=item.updated_at)  # type: ignore[arg-type]
            if merged is not existing_item:
                await self._items._update(session, existing_item, merged, allow_status_change=False)

    @staticmethod
    async def _find_approval_binding(
        session: AsyncSession,
        tenant_id: str,
        campaign_id: str,
    ) -> ApprovalBusinessBinding | None:
        result = await session.execute(
            text(
                "SELECT campaign_id, enrollment_version, link_version, selection_version, "
                "rule_snapshot_hash FROM campaign_approval_bindings WHERE tenant_id = "
                ":tenant_id AND campaign_id = :campaign_id"
            ),
            {"tenant_id": tenant_id, "campaign_id": campaign_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else ApprovalBusinessBinding.model_validate(dict(row))

    @staticmethod
    async def _write_approval_binding(
        session: AsyncSession,
        *,
        tenant_id: str,
        current: ApprovalBusinessBinding | None,
        updated: ApprovalBusinessBinding,
    ) -> None:
        values = {"tenant_id": tenant_id, **updated.model_dump()}
        if current is None:
            await session.execute(
                text(
                    "INSERT INTO campaign_approval_bindings (tenant_id, campaign_id, "
                    "enrollment_version, link_version, selection_version, rule_snapshot_hash) "
                    "VALUES (:tenant_id, :campaign_id, :enrollment_version, :link_version, "
                    ":selection_version, :rule_snapshot_hash)"
                ),
                values,
            )
            return
        if current == updated:
            return
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE campaign_approval_bindings SET enrollment_version = "
                    ":enrollment_version, link_version = :link_version, selection_version = "
                    ":selection_version, rule_snapshot_hash = :rule_snapshot_hash WHERE "
                    "tenant_id = :tenant_id AND campaign_id = :campaign_id AND "
                    "enrollment_version = :expected_enrollment_version AND link_version = "
                    ":expected_link_version AND selection_version = :expected_selection_version "
                    "AND rule_snapshot_hash = :expected_rule_snapshot_hash"
                ),
                values
                | {
                    "expected_enrollment_version": current.enrollment_version,
                    "expected_link_version": current.link_version,
                    "expected_selection_version": current.selection_version,
                    "expected_rule_snapshot_hash": current.rule_snapshot_hash,
                },
            ),
        )
        if result.rowcount != 1:
            raise BusinessRepositoryError("approval business binding optimistic lock conflict")

    async def load_enrollment_items(
        self,
        *,
        tenant_id: str,
        enrollment_item_ids: tuple[str, ...],
    ) -> tuple[tuple[EnrollmentItem, ...], tuple[ConfirmationTask, ...]]:
        items: list[EnrollmentItem] = []
        tasks: list[ConfirmationTask] = []
        try:
            async with self._sessions() as session:
                for item_id in enrollment_item_ids:
                    item = await self._items._find_by_id(session, item_id, tenant_id)
                    if item is None:
                        raise BusinessRepositoryError("enrollment item is unavailable")
                    items.append(item)
                    result = await session.execute(
                        text(
                            "SELECT tenant_id, confirmation_task_id, version, created_at, "
                            "updated_at, enrollment_item_id, subject_type, subject_id, sequence, "
                            "due_at, timeout_action, status FROM confirmation_tasks "
                            "WHERE tenant_id = :tenant_id AND enrollment_item_id = :item_id "
                            "ORDER BY sequence"
                        ),
                        {"tenant_id": tenant_id, "item_id": item_id},
                    )
                    tasks.extend(self._tasks._from_row(row) for row in result.mappings())
            return tuple(items), tuple(tasks)
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessRepositoryError("enrollment result read failed") from exc

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
    ) -> None:
        coupon = await self._coupons._find_by_id(session, coupon_batch_id, tenant_id)
        if coupon is None or coupon.status != "ready" or coupon.version != coupon_batch_version:
            raise BusinessRepositoryError("ready coupon batch is unavailable")
        campaign = await self._campaigns._find_by_id(session, coupon.campaign_id, tenant_id)
        if (
            campaign is None
            or campaign.status not in {"recruiting", "selecting"}
            or campaign.rule_snapshot_ref_id != rule_snapshot_ref_id
        ):
            raise BusinessRepositoryError("campaign is not eligible for coupon linking")
        current_binding = await self._find_approval_binding(
            session,
            tenant_id,
            coupon.campaign_id,
        )
        if (
            current_binding != expected_approval_binding
            or current_binding.rule_snapshot_hash != rule_snapshot_hash
            or product_criteria.rule_snapshot_hash != rule_snapshot_hash
        ):
            raise BusinessRepositoryError("coupon link approval business binding is stale")
        try:
            sellability_attestation.verify(current_products)
        except ValueError as exc:
            raise BusinessRepositoryError(
                "current product sellability attestation is invalid"
            ) from exc
        current_product_map = {
            (product.merchant_id, product.product_ref, product.product_version): product
            for product in current_products
        }
        validated: list[tuple[EnrollmentCouponLink, EnrollmentCouponLink | None]] = []
        for link in links:
            if link.tenant_id != tenant_id or link.coupon_batch_id != coupon_batch_id:
                raise BusinessRepositoryError("cross-tenant coupon link is forbidden")
            if link.benefit_tier not in allowed_tiers:
                raise BusinessRepositoryError("coupon benefit tier is not allowed")
            item = await self._items._find_by_id(session, link.enrollment_item_id, tenant_id)
            if item is None or item.campaign_id != coupon.campaign_id:
                raise BusinessRepositoryError("enrollment item does not match coupon campaign")
            if item.status != "confirmed":
                raise BusinessRepositoryError("enrollment item confirmation is incomplete")
            merchant = await self._load_merchant(session, tenant_id, item.merchant_id)
            if not EligibilityPolicy().evaluate(merchant, merchant_criteria).eligible:
                raise BusinessRepositoryError("coupon link merchant does not satisfy hard policy")
            product = await self._products._find_by_id(
                session,
                item.product_snapshot_id,
                tenant_id,
            )
            if product is None:
                raise BusinessRepositoryError("coupon link product snapshot is unavailable")
            self._validate_product_bundle(
                product,
                item,
                product_criteria=product_criteria,
                attestation=None,
            )
            current_product = current_product_map.get(
                (item.merchant_id, item.product_ref, item.product_version)
            )
            if (
                current_product is None
                or not ProductEligibilityPolicy()
                .evaluate(current_product, product_criteria)
                .eligible
            ):
                raise BusinessRepositoryError("coupon link product is not currently sellable")
            existing = await self._links._find_by_unique_key(session, link.unique_key(), tenant_id)
            if existing is not None and (
                existing.enrollment_coupon_link_id != link.enrollment_coupon_link_id
                or existing.status != "active"
            ):
                raise BusinessRepositoryError("coupon link business key conflicts")
            validated.append((link, existing))
        for link, existing in validated:
            if existing is None:
                await self._links._insert(session, link)
        await self._write_approval_binding(
            session,
            tenant_id=tenant_id,
            current=current_binding,
            updated=updated_approval_binding,
        )

    @staticmethod
    def _validate_enrollment_bundle(
        product: ProductSnapshot,
        enrollment: Enrollment,
        item: EnrollmentItem,
        tasks: tuple[ConfirmationTask, ...],
    ) -> None:
        if (
            enrollment.enrollment_id != item.enrollment_id
            or enrollment.merchant_id != item.merchant_id
            or product.merchant_id != item.merchant_id
            or product.product_ref != item.product_ref
            or product.product_version != item.product_version
            or product.product_snapshot_id != item.product_snapshot_id
            or any(task.enrollment_item_id != item.enrollment_item_id for task in tasks)
        ):
            raise BusinessRepositoryError("enrollment bundle associations do not match")

    @staticmethod
    async def _load_merchant(
        session: AsyncSession,
        tenant_id: str,
        merchant_id: str,
    ) -> MerchantRecord:
        result = await session.execute(
            text(
                "SELECT tenant_id, merchant_id, version, display_name, categories_json, "
                "cities_json, enrollment_systems_json, sales_org_code, active FROM merchants "
                "WHERE tenant_id = :tenant_id AND merchant_id = :merchant_id"
            ),
            {"tenant_id": tenant_id, "merchant_id": merchant_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise BusinessRepositoryError("enrollment merchant is unavailable")
        return MerchantRecord(
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

    @staticmethod
    def _validate_product_bundle(
        product: ProductSnapshot,
        item: EnrollmentItem,
        *,
        product_criteria: ProductEligibilityCriteria,
        attestation: EnrollmentEligibilityAttestation | None,
    ) -> None:
        if (
            product.product_snapshot_id != item.product_snapshot_id
            or product.merchant_id != item.merchant_id
            or product.product_ref != item.product_ref
            or product.product_version != item.product_version
        ):
            raise BusinessRepositoryError("product snapshot does not match enrollment item")
        attributes = product.attributes
        try:
            frozen_attestation = EnrollmentEligibilityAttestation.model_validate(
                attributes["eligibility_attestation"]
            )
            catalog_product = CatalogProductSnapshot.model_validate(
                {
                    "product_ref": product.product_ref,
                    "product_version": product.product_version,
                    "merchant_id": product.merchant_id,
                    "source_ref": attributes["source_ref_hash"],
                    "captured_at": attributes["captured_at"],
                    "category": attributes["category"],
                    "normalized_price": attributes["normalized_price"],
                    "currency": attributes["currency"],
                    "normalized_title": attributes["normalized_title"],
                    "keyword_labels": attributes["keyword_labels"],
                    "eligibility_facts": attributes["eligibility_facts"],
                }
            )
            sellability_value = attributes["sellability_snapshot"]
            if not isinstance(sellability_value, dict):
                raise TypeError("sellability snapshot must be an object")
            sellability = sellability_value
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessRepositoryError("product eligibility snapshot is incomplete") from exc
        if (
            frozen_attestation.rule_snapshot_hash != product_criteria.rule_snapshot_hash
            or frozen_attestation.product_policy_ref != product_criteria.policy_ref
            or frozen_attestation.product_policy_version != product_criteria.policy_version
            or frozen_attestation.catalog_snapshot_id != product.catalog_snapshot_id
            or (attestation is not None and frozen_attestation != attestation)
            or sellability.get("catalog_snapshot_id") != product.catalog_snapshot_id
            or sellability.get("product_version") != product.product_version
            or sellability.get("available") != catalog_product.eligibility_facts.get("available")
            or sellability.get("status") != catalog_product.eligibility_facts.get("status")
        ):
            raise BusinessRepositoryError("product eligibility snapshot binding does not match")
        if not ProductEligibilityPolicy().evaluate(catalog_product, product_criteria).eligible:
            raise BusinessRepositoryError("enrollment product does not satisfy hard policy")

    async def load_coupon_links(
        self,
        *,
        tenant_id: str,
        link_ids: tuple[str, ...],
    ) -> tuple[EnrollmentCouponLink, ...]:
        links: list[EnrollmentCouponLink] = []
        try:
            async with self._sessions() as session:
                for link_id in link_ids:
                    link = await self._links._find_by_id(session, link_id, tenant_id)
                    if link is None:
                        raise BusinessRepositoryError("coupon link is unavailable")
                    links.append(link)
            return tuple(links)
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessRepositoryError("coupon link result read failed") from exc

    async def load_confirmation_chain(
        self,
        *,
        tenant_id: str,
        confirmation_task_id: str,
    ) -> tuple[EnrollmentItem, tuple[ConfirmationTask, ...]]:
        try:
            async with self._sessions() as session:
                return await self._load_confirmation_chain(
                    session,
                    tenant_id=tenant_id,
                    confirmation_task_id=confirmation_task_id,
                )
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessRepositoryError("confirmation chain read failed") from exc

    async def apply_confirmation_chain(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        expected_item: EnrollmentItem,
        expected_tasks: tuple[ConfirmationTask, ...],
        updated_item: EnrollmentItem,
        updated_tasks: tuple[ConfirmationTask, ...],
    ) -> None:
        current_item, current_tasks = await self._load_confirmation_chain(
            session,
            tenant_id=tenant_id,
            confirmation_task_id=expected_tasks[0].confirmation_task_id,
        )
        if current_item != expected_item or current_tasks != expected_tasks:
            raise BusinessRepositoryError("confirmation chain optimistic lock conflict")
        if updated_item.tenant_id != tenant_id or any(
            task.tenant_id != tenant_id for task in updated_tasks
        ):
            raise BusinessRepositoryError("cross-tenant confirmation write is forbidden")
        if tuple(task.confirmation_task_id for task in updated_tasks) != tuple(
            task.confirmation_task_id for task in expected_tasks
        ):
            raise BusinessRepositoryError("confirmation chain identity is immutable")
        for existing, updated in zip(expected_tasks, updated_tasks, strict=True):
            if existing != updated:
                await self._tasks._update(
                    session,
                    existing,
                    updated,
                    allow_status_change=True,
                )
        if expected_item != updated_item:
            await self._items._update(
                session,
                expected_item,
                updated_item,
                allow_status_change=True,
            )

    async def _load_confirmation_chain(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        confirmation_task_id: str,
    ) -> tuple[EnrollmentItem, tuple[ConfirmationTask, ...]]:
        requested = await self._tasks._find_by_id(session, confirmation_task_id, tenant_id)
        if requested is None:
            raise BusinessRepositoryError("confirmation task is unavailable")
        item = await self._items._find_by_id(session, requested.enrollment_item_id, tenant_id)
        if item is None:
            raise BusinessRepositoryError("confirmation enrollment item is unavailable")
        result = await session.execute(
            text(
                "SELECT tenant_id, confirmation_task_id, version, created_at, updated_at, "
                "enrollment_item_id, subject_type, subject_id, sequence, due_at, "
                "timeout_action, status FROM confirmation_tasks WHERE tenant_id = :tenant_id "
                "AND enrollment_item_id = :item_id ORDER BY sequence"
            ),
            {"tenant_id": tenant_id, "item_id": item.enrollment_item_id},
        )
        tasks = tuple(self._tasks._from_row(row) for row in result.mappings())
        if not tasks:
            raise BusinessRepositoryError("confirmation chain is unavailable")
        return item, tasks


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


class SQLiteCampaignDraftRepository:
    """Persist all validated draft facts in one Business DB transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._rule_refs = SQLiteCampaignRuleSnapshotRefRepository(sessions)
        self._campaigns = SQLiteCampaignRepository(sessions)
        self._coupons = SQLiteCouponBatchRepository(sessions)
        self._publications = SQLiteRecruitmentPublicationRepository(sessions)

    async def create_bundle(
        self,
        *,
        rule_snapshot_ref: CampaignRuleSnapshotRef,
        campaign: Campaign,
        coupon_batch: CouponBatch,
        recruitment_publication: RecruitmentPublication,
        ctx: Context,
    ) -> None:
        tenant_id = getattr(ctx, "tenant_id", None)
        entities = (rule_snapshot_ref, campaign, coupon_batch, recruitment_publication)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise BusinessRepositoryError("trusted tenant context is required")
        if any(entity.tenant_id != tenant_id for entity in entities):
            raise BusinessRepositoryError("cross-tenant business write is forbidden")
        campaign.validate_tenant_links(rule_snapshot_ref, coupon_batch, recruitment_publication)
        try:
            async with self._sessions.begin() as session:
                await self._rule_refs._insert(session, rule_snapshot_ref)
                await self._campaigns._insert(session, campaign)
                await self._coupons._insert(session, coupon_batch)
                await self._publications._insert(session, recruitment_publication)
        except BusinessRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise BusinessRepositoryError("campaign draft persistence failed") from exc


class SQLiteCampaignLaunchRepository:
    """Business persistence needed by the checkpointed launch saga."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._campaigns = SQLiteCampaignRepository(sessions)
        self._rule_refs = SQLiteCampaignRuleSnapshotRefRepository(sessions)
        self._coupons = SQLiteCouponBatchRepository(sessions)
        self._publications = SQLiteRecruitmentPublicationRepository(sessions)
        self._sagas = SQLiteLaunchSagaStateRepository(sessions)

    async def load_draft_entities(
        self,
        *,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        coupon_batch_id: str,
        recruitment_publication_id: str,
        ctx: Context,
    ) -> tuple[Campaign, CampaignRuleSnapshotRef, CouponBatch, RecruitmentPublication]:
        campaign = await self._campaigns.get(campaign_id, ctx)
        rule_ref = await self._rule_refs.get(rule_snapshot_ref_id, ctx)
        coupon = await self._coupons.get(coupon_batch_id, ctx)
        publication = await self._publications.get(recruitment_publication_id, ctx)
        if campaign is None or rule_ref is None or coupon is None or publication is None:
            raise BusinessRepositoryError("campaign launch draft is unavailable")
        return campaign, rule_ref, coupon, publication

    async def get_saga(self, campaign_id: str, ctx: Context) -> LaunchSagaState | None:
        return await self._sagas.get_by_unique_key((ctx.tenant_id, campaign_id), ctx)

    async def create_saga(self, saga: LaunchSagaState, ctx: Context) -> LaunchSagaState:
        return await self._sagas.create(saga, ctx)

    async def transition_saga(
        self,
        saga: LaunchSagaState,
        target: LaunchSagaStatus,
        updated_at: datetime,
        ctx: Context,
    ) -> LaunchSagaState:
        return await self._sagas.transition(saga.launch_saga_id, target, updated_at, ctx)

    async def mark_coupon_ready(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        coupon_batch_id: str,
        updated_at: datetime,
    ) -> None:
        existing = await self._coupons._find_by_id(session, coupon_batch_id, tenant_id)
        if existing is None:
            raise BusinessRepositoryError("coupon batch is unavailable")
        current = existing
        if current.status == "draft":
            materializing = current.transition_to("materializing", updated_at=updated_at)
            await self._coupons._update(session, current, materializing, allow_status_change=True)
            current = materializing
        if current.status != "materializing":
            raise BusinessRepositoryError("coupon batch is not materializable")
        ready = current.transition_to("ready", updated_at=updated_at)
        await self._coupons._update(session, current, ready, allow_status_change=True)

    async def mark_recruitment_published(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        recruitment_publication_id: str,
        request_id: str,
        receipt_id: str,
        updated_at: datetime,
    ) -> None:
        existing = await self._publications._find_by_id(
            session, recruitment_publication_id, tenant_id
        )
        if existing is None:
            raise BusinessRepositoryError("recruitment publication is unavailable")
        if existing.status != "pending":
            raise BusinessRepositoryError("recruitment publication is not publishable")
        published = existing._next_version(
            updated_at=updated_at,
            status="published",
            request_id=request_id,
            receipt_id=receipt_id,
        )
        await self._publications._update(session, existing, published, allow_status_change=True)
