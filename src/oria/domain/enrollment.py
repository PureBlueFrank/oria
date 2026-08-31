"""Enrollment item aggregation and coupon-link domain services."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from pydantic import Field, field_validator, model_validator

from oria.adapters.products import ProductCatalogAdapter, ProductCatalogPolicyBinding
from oria.core.execution_ledger import ExecutionEventBundle, ExecutionLedger
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    EventEnvelope,
    JsonValue,
    ResourceRef,
    ValueModel,
)
from oria.domain.business import (
    Campaign,
    ConfirmationTask,
    Enrollment,
    EnrollmentCouponLink,
    EnrollmentItem,
    EnrollmentSource,
)
from oria.domain.business import (
    ProductSnapshot as BusinessProductSnapshot,
)
from oria.domain.confirmations import BusinessConfirmationPolicy, ConfirmationSubjectType
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.ledger import DomainEvent, OutboxRecord, ToolExecution
from oria.domain.models import EligibilityCriteria
from oria.domain.product_eligibility import (
    ProductEligibilityCriteria,
    ProductEligibilityPolicy,
    ProductSnapshot,
)
from oria.domain.repositories import (
    CampaignRepository,
    CampaignRuleSnapshotRefRepository,
    CouponBatchRepository,
    EnrollmentWorkflowRepository,
    MerchantRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from oria.core.context import Context
    from oria.rag.models import CampaignRuleSnapshot

BenefitTier: TypeAlias = Literal["base", "boosted"]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class EnrollmentItemInput(ValueModel):
    merchant_id: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    product_version: str = Field(min_length=1)


class UpsertEnrollmentItemsArgs(ValueModel):
    campaign_id: str = Field(min_length=1)
    source: EnrollmentSource
    items: tuple[EnrollmentItemInput, ...] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("items")
    @classmethod
    def require_unique_item_keys(
        cls, value: tuple[EnrollmentItemInput, ...]
    ) -> tuple[EnrollmentItemInput, ...]:
        keys = {(item.merchant_id, item.product_ref, item.product_version) for item in value}
        if len(keys) != len(value):
            raise ValueError("enrollment item business keys must be unique within one request")
        return value


class UpsertEnrollmentItemsResult(ValueModel):
    schema_version: Literal[1] = 1
    campaign_id: str
    source: EnrollmentSource
    enrollment_items: tuple[EnrollmentItem, ...]
    confirmation_tasks: tuple[ConfirmationTask, ...]
    execution_id: str
    idempotency_key: str


class LinkCouponBatchArgs(ValueModel):
    enrollment_item_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    coupon_batch_id: str = Field(min_length=1)
    tier_mapping: dict[str, BenefitTier] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_exact_tier_mapping(self) -> LinkCouponBatchArgs:
        if len(self.enrollment_item_ids) != len(set(self.enrollment_item_ids)):
            raise ValueError("enrollment_item_ids must be unique")
        if set(self.tier_mapping) != set(self.enrollment_item_ids):
            raise ValueError("tier_mapping must cover exactly the enrollment item ids")
        return self


class LinkCouponBatchResult(ValueModel):
    schema_version: Literal[1] = 1
    coupon_batch_id: str
    links: tuple[EnrollmentCouponLink, ...]
    execution_id: str
    idempotency_key: str


class ConfirmationSubjectDirectory(Protocol):
    async def resolve(
        self,
        *,
        tenant_id: str,
        merchant_id: str,
    ) -> Mapping[ConfirmationSubjectType, str]: ...


class InMemoryConfirmationSubjectDirectory:
    """Trusted synthetic identity mapping; callers cannot provide assignees."""

    def __init__(
        self,
        assignments: Mapping[tuple[str, str], Mapping[ConfirmationSubjectType, str]],
    ) -> None:
        self._assignments = {key: dict(subjects) for key, subjects in assignments.items()}

    async def resolve(
        self,
        *,
        tenant_id: str,
        merchant_id: str,
    ) -> Mapping[ConfirmationSubjectType, str]:
        return dict(self._assignments.get((tenant_id, merchant_id), {}))


class RuleSnapshotReader(Protocol):
    async def get(self, snapshot_id: str, ctx: Context) -> CampaignRuleSnapshot: ...


class EnrollmentService:
    """Reauthorize and atomically persist one deterministic enrollment write."""

    def __init__(
        self,
        *,
        campaigns: CampaignRepository,
        rule_refs: CampaignRuleSnapshotRefRepository,
        rule_snapshots: RuleSnapshotReader,
        merchants: MerchantRepository,
        repository: EnrollmentWorkflowRepository,
        catalog: ProductCatalogAdapter,
        ledger: ExecutionLedger,
        subjects: ConfirmationSubjectDirectory,
        merchant_eligibility: EligibilityPolicy | None = None,
        product_eligibility: ProductEligibilityPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        confirmation_ttl: timedelta = timedelta(days=1),
    ) -> None:
        self._campaigns = campaigns
        self._rule_refs = rule_refs
        self._rule_snapshots = rule_snapshots
        self._merchants = merchants
        self._repository = repository
        self._catalog = catalog
        self._ledger = ledger
        self._subjects = subjects
        self._merchant_eligibility = merchant_eligibility or EligibilityPolicy()
        self._product_eligibility = product_eligibility or ProductEligibilityPolicy()
        self._clock = clock
        self._confirmation_ttl = confirmation_ttl

    async def upsert_items(
        self,
        request: UpsertEnrollmentItemsArgs,
        ctx: Context,
        *,
        new_enrollment_version: bool = False,
    ) -> UpsertEnrollmentItemsResult:
        await _authorize("enrollment:item:write", "campaign", request.campaign_id, ctx)
        campaign, snapshot = await self._campaign_snapshot(request.campaign_id, ctx)
        rule = snapshot.enrollment_policy
        if campaign.status != "recruiting":
            raise ValueError("campaign is not accepting enrollment items")
        if request.source not in rule.accepted_sources:
            raise PermissionError("enrollment source is not accepted by the frozen rule")
        if request.source == "merchant" and campaign.enrollment_mode == "auto":
            raise PermissionError("merchant source is incompatible with auto enrollment mode")
        if request.source == "auto" and campaign.enrollment_mode == "merchant":
            raise PermissionError("auto source is incompatible with merchant enrollment mode")
        merchant_records = {
            record.merchant_id: record for record in await self._merchants.list_for_eligibility(ctx)
        }
        merchant_criteria = self._merchant_criteria(snapshot)
        for merchant_id in {item.merchant_id for item in request.items}:
            record = merchant_records.get(merchant_id)
            if (
                record is None
                or not self._merchant_eligibility.evaluate(record, merchant_criteria).eligible
            ):
                raise PermissionError("enrollment merchant is not eligible")
        product_criteria = ProductEligibilityCriteria.from_snapshot(snapshot)
        catalog_snapshot_id, products = await self._load_products(
            request.items,
            product_criteria,
            ctx,
        )
        now = self._now()
        confirmation_policy = BusinessConfirmationPolicy.from_snapshot(snapshot)
        bundles: list[
            tuple[
                BusinessProductSnapshot,
                Enrollment,
                EnrollmentItem,
                tuple[ConfirmationTask, ...],
            ]
        ] = []
        for item_request in request.items:
            key = (
                item_request.merchant_id,
                item_request.product_ref,
                item_request.product_version,
            )
            product = products[key]
            decision = self._product_eligibility.evaluate(product, product_criteria)
            if not decision.eligible:
                raise ValueError("enrollment product does not satisfy the frozen hard policy")
            product_snapshot_id = _stable_id(
                "product_snapshot",
                ctx.tenant_id,
                product.product_ref,
                product.product_version,
            )
            enrollment_id = _stable_id(
                "enrollment", ctx.tenant_id, campaign.campaign_id, product.merchant_id
            )
            enrollment_item_id = _stable_id(
                "enrollment_item",
                ctx.tenant_id,
                campaign.campaign_id,
                product.merchant_id,
                product.product_ref,
                product.product_version,
            )
            business_snapshot = BusinessProductSnapshot(
                tenant_id=ctx.tenant_id,
                product_snapshot_id=product_snapshot_id,
                product_ref=product.product_ref,
                product_version=product.product_version,
                catalog_snapshot_id=catalog_snapshot_id,
                attributes=self._redacted_attributes(product),
                version=1,
                created_at=now,
                updated_at=now,
            )
            enrollment = Enrollment(
                tenant_id=ctx.tenant_id,
                enrollment_id=enrollment_id,
                campaign_id=campaign.campaign_id,
                merchant_id=product.merchant_id,
                mode=campaign.enrollment_mode,
                status="submitted",
                version=1,
                created_at=now,
                updated_at=now,
            )
            enrollment_item = EnrollmentItem(
                tenant_id=ctx.tenant_id,
                enrollment_item_id=enrollment_item_id,
                enrollment_id=enrollment_id,
                campaign_id=campaign.campaign_id,
                merchant_id=product.merchant_id,
                product_ref=product.product_ref,
                product_version=product.product_version,
                product_snapshot_id=product_snapshot_id,
                mode=campaign.enrollment_mode,
                sources=frozenset({request.source}),
                status=(
                    "pending_confirmation" if confirmation_policy.ordered_steps else "confirmed"
                ),
                version=1,
                created_at=now,
                updated_at=now,
            )
            subject_ids = await self._subjects.resolve(
                tenant_id=ctx.tenant_id,
                merchant_id=product.merchant_id,
            )
            tasks = confirmation_policy.generate_tasks(
                enrollment_item=enrollment_item,
                subject_ids=subject_ids,
                created_at=now,
                due_at=now + self._confirmation_ttl,
            )
            bundles.append((business_snapshot, enrollment, enrollment_item, tasks))
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name="upsert_enrollment_items",
            tool_schema_version=1,
            schema=UpsertEnrollmentItemsArgs,
            args=request.model_dump(),
            stable_business_id=f"{request.campaign_id}:{request.idempotency_key}",
            checkpoint_id=ctx.run_id,
        )
        if execution.status == "reserved":
            await _authorize("enrollment:item:write", "campaign", request.campaign_id, ctx)
            executing = await self._ledger.mark_executing(execution)

            async def write(session: AsyncSession) -> None:
                await self._repository.upsert_enrollment_items(
                    session,
                    tenant_id=ctx.tenant_id,
                    campaign_id=request.campaign_id,
                    rule_snapshot_ref_id=campaign.rule_snapshot_ref_id,
                    source=request.source,
                    bundles=tuple(bundles),
                    new_enrollment_version=new_enrollment_version,
                )

            events = self._events(
                execution=executing,
                aggregate_type="campaign",
                aggregate_id=request.campaign_id,
                event_type="enrollment.items_upserted",
                count=len(request.items),
                ctx=ctx,
            )
            execution = await self._ledger.record_success(
                executing,
                receipt_id=_stable_id("receipt", executing.idempotency_key),
                business_write=write,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
        elif execution.status != "succeeded":
            raise RuntimeError("prior enrollment execution has not completed successfully")
        item_ids = tuple(bundle[2].enrollment_item_id for bundle in bundles)
        items, tasks = await self._repository.load_enrollment_items(
            tenant_id=ctx.tenant_id,
            enrollment_item_ids=item_ids,
        )
        return UpsertEnrollmentItemsResult(
            campaign_id=request.campaign_id,
            source=request.source,
            enrollment_items=items,
            confirmation_tasks=tasks,
            execution_id=execution.execution_id,
            idempotency_key=request.idempotency_key,
        )

    async def _campaign_snapshot(
        self,
        campaign_id: str,
        ctx: Context,
    ) -> tuple[Campaign, CampaignRuleSnapshot]:
        campaign = await self._campaigns.get(campaign_id, ctx)
        if campaign is None:
            raise LookupError("campaign is unavailable")
        rule_ref = await self._rule_refs.get(campaign.rule_snapshot_ref_id, ctx)
        if rule_ref is None:
            raise LookupError("campaign rule snapshot reference is unavailable")
        snapshot = await self._rule_snapshots.get(rule_ref.snapshot_id, ctx)
        if (
            snapshot.tenant_id != ctx.tenant_id
            or snapshot.snapshot_hash != rule_ref.snapshot_hash
            or snapshot.recompute_hash() != snapshot.snapshot_hash
        ):
            raise PermissionError("campaign rule snapshot binding does not match")
        return campaign, snapshot

    @staticmethod
    def _merchant_criteria(snapshot: CampaignRuleSnapshot) -> EligibilityCriteria:
        scope = snapshot.recruitment_scope
        return EligibilityCriteria(
            rule_set_id=snapshot.snapshot_id,
            rule_version=snapshot.snapshot_hash,
            categories=scope.categories,
            cities=scope.cities,
            enrollment_systems=scope.enrollment_systems,
            allowlist_merchant_ids=tuple(sorted(scope.internal_allowlist())),
            denylist_merchant_ids=tuple(sorted(scope.internal_denylist())),
            sales_org_scope=tuple(sorted(scope.internal_sales_org_scope())),
        )

    async def _load_products(
        self,
        requested: tuple[EnrollmentItemInput, ...],
        criteria: ProductEligibilityCriteria,
        ctx: Context,
    ) -> tuple[str, dict[tuple[str, str, str], ProductSnapshot]]:
        merchants = tuple(sorted({item.merchant_id for item in requested}))
        binding = ProductCatalogPolicyBinding(
            policy_ref=criteria.policy_ref,
            policy_version=criteria.policy_version,
        )
        cursor: str | None = None
        catalog_snapshot_id: str | None = None
        found: dict[tuple[str, str, str], ProductSnapshot] = {}
        while True:
            page = await self._catalog.list_products(
                tenant_id=ctx.tenant_id,
                merchant_ids=merchants,
                policy=binding,
                cursor=cursor,
                limit=100,
            )
            if catalog_snapshot_id is None:
                catalog_snapshot_id = page.catalog_snapshot_id
            elif page.catalog_snapshot_id != catalog_snapshot_id:
                raise RuntimeError("product catalog pagination changed snapshots")
            for product in page.products:
                found[(product.merchant_id, product.product_ref, product.product_version)] = product
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        requested_keys = {
            (item.merchant_id, item.product_ref, item.product_version) for item in requested
        }
        if not requested_keys.issubset(found):
            raise LookupError("enrollment product snapshot is unavailable")
        if catalog_snapshot_id is None:
            raise LookupError("product catalog snapshot is unavailable")
        return catalog_snapshot_id, {key: found[key] for key in requested_keys}

    @staticmethod
    def _redacted_attributes(product: ProductSnapshot) -> dict[str, JsonValue]:
        return {
            "captured_at": product.captured_at.isoformat(),
            "category": product.category,
            "currency": product.currency,
            "normalized_price": str(product.normalized_price),
            "source_ref_hash": "sha256:"
            + hashlib.sha256(product.source_ref.encode("utf-8")).hexdigest(),
        }

    def _events(
        self,
        *,
        execution: ToolExecution,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        count: int,
        ctx: Context,
    ) -> ExecutionEventBundle:
        return _execution_events(
            now=self._now(),
            execution=execution,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            count=count,
            ctx=ctx,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enrollment service clock must return a timezone-aware time")
        return value


class CouponLinkService:
    def __init__(
        self,
        *,
        repository: EnrollmentWorkflowRepository,
        ledger: ExecutionLedger,
        campaigns: CampaignRepository,
        coupons: CouponBatchRepository,
        rule_refs: CampaignRuleSnapshotRefRepository,
        rule_snapshots: RuleSnapshotReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._campaigns = campaigns
        self._coupons = coupons
        self._rule_refs = rule_refs
        self._rule_snapshots = rule_snapshots
        self._clock = clock

    async def link(
        self,
        request: LinkCouponBatchArgs,
        ctx: Context,
    ) -> LinkCouponBatchResult:
        await _authorize("enrollment:coupon:link", "coupon_batch", request.coupon_batch_id, ctx)
        coupon = await self._coupons.get(request.coupon_batch_id, ctx)
        if coupon is None or coupon.status != "ready":
            raise LookupError("ready coupon batch is unavailable")
        campaign = await self._campaigns.get(coupon.campaign_id, ctx)
        if campaign is None:
            raise LookupError("coupon campaign is unavailable")
        rule_ref = await self._rule_refs.get(campaign.rule_snapshot_ref_id, ctx)
        if rule_ref is None:
            raise LookupError("coupon campaign rule snapshot is unavailable")
        snapshot = await self._rule_snapshots.get(rule_ref.snapshot_id, ctx)
        if (
            snapshot.tenant_id != ctx.tenant_id
            or snapshot.snapshot_hash != rule_ref.snapshot_hash
            or snapshot.recompute_hash() != snapshot.snapshot_hash
        ):
            raise PermissionError("coupon campaign rule snapshot binding does not match")
        allowed_tiers = frozenset(snapshot.benefit_policy.tiers)
        if any(tier not in allowed_tiers for tier in request.tier_mapping.values()):
            raise ValueError("coupon benefit tier is not allowed by the frozen rule")
        now = self._now()
        candidates = tuple(
            EnrollmentCouponLink(
                tenant_id=ctx.tenant_id,
                enrollment_coupon_link_id=_stable_id(
                    "enrollment_coupon_link",
                    ctx.tenant_id,
                    item_id,
                    request.coupon_batch_id,
                    request.tier_mapping[item_id],
                ),
                enrollment_item_id=item_id,
                coupon_batch_id=request.coupon_batch_id,
                benefit_tier=request.tier_mapping[item_id],
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
            for item_id in request.enrollment_item_ids
        )
        execution = await self._ledger.reserve_for_args(
            execution_id=f"tool_execution_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            tool_name="link_coupon_batch",
            tool_schema_version=1,
            schema=LinkCouponBatchArgs,
            args=request.model_dump(),
            stable_business_id=f"{request.coupon_batch_id}:{request.idempotency_key}",
            checkpoint_id=ctx.run_id,
        )
        if execution.status == "reserved":
            await _authorize("enrollment:coupon:link", "coupon_batch", request.coupon_batch_id, ctx)
            executing = await self._ledger.mark_executing(execution)

            async def write(session: AsyncSession) -> None:
                await self._repository.link_coupon_batch(
                    session,
                    tenant_id=ctx.tenant_id,
                    coupon_batch_id=request.coupon_batch_id,
                    coupon_batch_version=coupon.version,
                    rule_snapshot_ref_id=campaign.rule_snapshot_ref_id,
                    allowed_tiers=allowed_tiers,
                    links=candidates,
                )

            events = _execution_events(
                now=self._now(),
                execution=executing,
                aggregate_type="coupon_batch",
                aggregate_id=request.coupon_batch_id,
                event_type="enrollment.coupon_batch_linked",
                count=len(candidates),
                ctx=ctx,
            )
            execution = await self._ledger.record_success(
                executing,
                receipt_id=_stable_id("receipt", executing.idempotency_key),
                business_write=write,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
        elif execution.status != "succeeded":
            raise RuntimeError("prior coupon-link execution has not completed successfully")
        links = await self._repository.load_coupon_links(
            tenant_id=ctx.tenant_id,
            link_ids=tuple(item.enrollment_coupon_link_id for item in candidates),
        )
        return LinkCouponBatchResult(
            coupon_batch_id=request.coupon_batch_id,
            links=links,
            execution_id=execution.execution_id,
            idempotency_key=request.idempotency_key,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coupon link service clock must return a timezone-aware time")
        return value


async def _authorize(
    action: str,
    resource_type: str,
    resource_id: str,
    ctx: Context,
) -> None:
    decision = await ctx.policy.authorize(
        AuthorizationRequest(
            actor=ctx.actor,
            executor=ctx.executor,
            action=action,
            resource=ResourceRef(
                resource_type=resource_type,
                resource_id=resource_id,
                tenant_id=ctx.tenant_id,
            ),
            context=AuthorizationContext(correlation_id=ctx.correlation_id),
        ),
        ctx,
    )
    if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
        raise PermissionError("enrollment write is not authorized")


def _execution_events(
    *,
    now: datetime,
    execution: ToolExecution,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    count: int,
    ctx: Context,
) -> ExecutionEventBundle:
    event_id = f"domain_event_{uuid.uuid4().hex}"
    payload: dict[str, JsonValue] = {
        "args_hash": execution.canonical_args_hash,
        "count": count,
        "execution_id": execution.execution_id,
    }
    return ExecutionEventBundle(
        domain_events=(
            DomainEvent(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                event_version=1,
                payload=payload,
                occurred_at=now,
                correlation_id=ctx.correlation_id,
            ),
        ),
        audit_events=(
            EventEnvelope(
                event_id=f"business_audit_{uuid.uuid4().hex}",
                occurred_at=now,
                tenant_id=ctx.tenant_id,
                actor=ctx.actor.subject_id,
                action=execution.tool_name,
                resource=ResourceRef(
                    resource_type=aggregate_type,
                    resource_id=aggregate_id,
                    tenant_id=ctx.tenant_id,
                ),
                decision="allow",
                policy_version="frozen-campaign-rule/v1",
                args_hash=execution.canonical_args_hash,
                result="success",
                correlation_id=ctx.correlation_id,
                payload={"count": count, "execution_id": execution.execution_id},
            ),
        ),
        outbox_records=(
            OutboxRecord(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                topic=event_type,
                payload_json=json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                occurred_at=now,
                available_at=now,
            ),
        ),
    )
