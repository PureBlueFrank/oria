"""Reusable synthetic SQLite harness for V0.3-T05 enrollment tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from oria.adapters.products import InMemoryProductCatalogAdapter
from oria.config import resolve_runtime_config
from oria.core.execution_ledger import ExecutionLedger
from oria.core.types import Principal
from oria.data import initialize_data
from oria.domain.business import Campaign, CampaignRuleSnapshotRef, CouponBatch
from oria.domain.enrollment import (
    AutoCircleRunBinding,
    AutoEnrollmentCommand,
    CouponLinkService,
    EnrollmentItemInput,
    EnrollmentService,
    InMemoryConfirmationSubjectDirectory,
)
from oria.domain.models import BenefitRule, ConfirmationRule
from oria.domain.product_eligibility import ProductEligibilityPolicy, ProductSnapshot
from oria.domain.products import ProductQueryService
from oria.permission.local import LocalPolicyEngine
from oria.rag.models import CampaignRuleSnapshot
from oria.resources.loader import load_demo_data
from oria.storage.database import DatabaseResources
from oria.storage.repositories import (
    SQLiteCampaignRepository,
    SQLiteCampaignRuleSnapshotRefRepository,
    SQLiteCouponBatchRepository,
    SQLiteEnrollmentWorkflowRepository,
    SQLiteMerchantRepository,
)

NOW = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
TENANT = "local-community"

ADMIN = Principal(
    subject_id="campaign-admin",
    tenant_id=TENANT,
    kind="human",
    roles=("campaign_admin",),
    authn_method="trusted-test-profile",
)
EXECUTOR = Principal(
    subject_id="enrollment-worker",
    tenant_id=TENANT,
    kind="service",
    roles=("runtime",),
    authn_method="trusted-test-profile",
)


class RuleStore:
    def __init__(self, snapshot: CampaignRuleSnapshot) -> None:
        self.snapshot = snapshot

    async def get(self, snapshot_id: str, ctx: Any) -> CampaignRuleSnapshot:
        if snapshot_id != self.snapshot.snapshot_id or ctx.tenant_id != self.snapshot.tenant_id:
            raise LookupError("rule snapshot is unavailable")
        return self.snapshot


def product(
    product_ref: str = "product-1",
    *,
    merchant_id: str = "demo-m001",
    product_version: str = "v1",
    price: str = "100.00",
    category: str = "餐饮套餐",
    labels: tuple[str, ...] = ("夏季", "套餐"),
    available: bool = True,
) -> ProductSnapshot:
    return ProductSnapshot(
        product_ref=product_ref,
        product_version=product_version,
        merchant_id=merchant_id,
        source_ref=f"synthetic://catalog/{product_ref}/{product_version}",
        captured_at=NOW,
        category=category,
        normalized_price=Decimal(price),
        currency="CNY",
        normalized_title=f"合成商品 {product_ref}",
        keyword_labels=labels,
        eligibility_facts={"available": available, "status": "available" if available else "off"},
    )


def auto_command(
    items: tuple[EnrollmentItemInput, ...],
    *,
    circle_run_id: str,
    campaign_id: str = "campaign-1",
    catalog_snapshot_id: str = "catalog-snapshot-v1",
) -> AutoEnrollmentCommand:
    binding = AutoCircleRunBinding.for_items(
        campaign_id=campaign_id,
        circle_run_id=circle_run_id,
        product_circle_policy_ref="synthetic-product-circle-policy",
        product_circle_policy_version="1.0.0",
        catalog_snapshot_id=catalog_snapshot_id,
        items=items,
    )
    return AutoEnrollmentCommand(campaign_id=campaign_id, items=items, binding=binding)


def snapshot(
    *,
    mode: Literal["merchant", "auto", "hybrid"] = "hybrid",
    late_event_action: Literal["reject", "new_version"] = "reject",
    confirmation_steps: tuple[Literal["merchant", "sales", "sales_manager"], ...] = (
        "merchant",
        "sales",
        "sales_manager",
    ),
    benefit_tiers: tuple[Literal["base", "boosted"], ...] = ("base", "boosted"),
) -> CampaignRuleSnapshot:
    rules = load_demo_data().rules
    accepted_sources: tuple[Literal["merchant", "auto"], ...]
    if mode == "merchant":
        accepted_sources = ("merchant",)
    elif mode == "auto":
        accepted_sources = ("auto",)
    else:
        accepted_sources = ("merchant", "auto")
    enrollment = rules.enrollment_policy.model_copy(
        update={
            "mode": mode,
            "accepted_sources": accepted_sources,
            "late_event_action": late_event_action,
        }
    )
    benefit = BenefitRule(
        tiers=benefit_tiers,
        tier_rules=tuple(
            rule for rule in rules.benefit_policy.tier_rules if rule.name in benefit_tiers
        ),
        currency=rules.benefit_policy.currency,
        rounding=rules.benefit_policy.rounding,
        budget_cap=rules.benefit_policy.budget_cap,
    )
    placeholder = CampaignRuleSnapshot(
        snapshot_id="rs_123456789012345678901234",
        snapshot_hash="sha256:" + "0" * 64,
        tenant_id=TENANT,
        effective_at=NOW,
        basic=rules.basic,
        recruitment_scope=rules.recruitment_scope,
        enrollment_policy=enrollment,
        benefit_policy=benefit,
        confirmation_policy=ConfirmationRule(
            ordered_steps=confirmation_steps,
            timeout_action="reject",
        ),
        merchant_material=rules.merchant_material,
        field_evidence={},
    )
    return placeholder.model_copy(update={"snapshot_hash": placeholder.recompute_hash()})


@dataclass(slots=True)
class EnrollmentHarness:
    databases: DatabaseResources
    ctx: SimpleNamespace
    policy: LocalPolicyEngine
    snapshot: CampaignRuleSnapshot
    catalog: InMemoryProductCatalogAdapter
    query: ProductQueryService
    enrollments: EnrollmentService
    links: CouponLinkService
    workflow_repository: SQLiteEnrollmentWorkflowRepository


@asynccontextmanager
async def enrollment_harness(
    tmp_path: Path,
    *,
    mode: Literal["merchant", "auto", "hybrid"] = "hybrid",
    late_event_action: Literal["reject", "new_version"] = "reject",
    confirmation_steps: tuple[Literal["merchant", "sales", "sales_manager"], ...] = (
        "merchant",
        "sales",
        "sales_manager",
    ),
    benefit_tiers: tuple[Literal["base", "boosted"], ...] = ("base", "boosted"),
    products: tuple[ProductSnapshot, ...] | None = None,
    actor: Principal = ADMIN,
) -> AsyncIterator[EnrollmentHarness]:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    frozen = snapshot(
        mode=mode,
        late_event_action=late_event_action,
        confirmation_steps=confirmation_steps,
        benefit_tiers=benefit_tiers,
    )
    policy = LocalPolicyEngine(trusted_actors=(actor,), trusted_executors=(EXECUTOR,))
    ctx = SimpleNamespace(
        actor=actor,
        executor=EXECUTOR,
        tenant_id=actor.tenant_id,
        correlation_id="enrollment-correlation",
        run_id="enrollment-run",
        policy=policy,
    )
    async with DatabaseResources(config) as databases:
        campaigns = SQLiteCampaignRepository(databases.business_sessions)
        rule_refs = SQLiteCampaignRuleSnapshotRefRepository(databases.business_sessions)
        coupons = SQLiteCouponBatchRepository(databases.business_sessions)
        common = {
            "tenant_id": TENANT,
            "version": 1,
            "created_at": NOW,
            "updated_at": NOW,
        }
        await rule_refs.create(
            CampaignRuleSnapshotRef(
                **common,
                campaign_rule_snapshot_ref_id="rule-ref-1",
                snapshot_id=frozen.snapshot_id,
                snapshot_hash=frozen.snapshot_hash,
            ),
            ctx,  # type: ignore[arg-type]
        )
        await campaigns.create(
            Campaign(
                **common,
                campaign_id="campaign-1",
                rule_snapshot_ref_id="rule-ref-1",
                enrollment_mode=mode,
                status="recruiting",
            ),
            ctx,  # type: ignore[arg-type]
        )
        await coupons.create(
            CouponBatch(
                **common,
                coupon_batch_id="coupon-1",
                campaign_id="campaign-1",
                coupon_spec_hash="sha256:" + "c" * 64,
                status="ready",
            ),
            ctx,  # type: ignore[arg-type]
        )
        catalog = InMemoryProductCatalogAdapter(
            {TENANT: products or (product(), product("product-2"))}
        )
        rule_store = RuleStore(frozen)
        merchants = SQLiteMerchantRepository(databases.business_sessions)
        workflow_repository = SQLiteEnrollmentWorkflowRepository(databases.business_sessions)
        ledger = ExecutionLedger(databases.business_sessions, clock=lambda: NOW)
        enrollments = EnrollmentService(
            campaigns=campaigns,
            rule_refs=rule_refs,
            rule_snapshots=rule_store,
            merchants=merchants,
            repository=workflow_repository,
            catalog=catalog,
            ledger=ledger,
            subjects=InMemoryConfirmationSubjectDirectory(
                {
                    (TENANT, "demo-m001"): {
                        "merchant": "demo-m001",
                        "sales": "sales-1",
                        "sales_manager": "manager-1",
                    }
                }
            ),
            clock=lambda: NOW,
        )
        yield EnrollmentHarness(
            databases=databases,
            ctx=ctx,
            policy=policy,
            snapshot=frozen,
            catalog=catalog,
            query=ProductQueryService(
                campaigns=campaigns,
                rule_refs=rule_refs,
                rule_snapshots=rule_store,
                catalog=catalog,
                eligibility=ProductEligibilityPolicy(),
            ),
            enrollments=enrollments,
            links=CouponLinkService(
                repository=workflow_repository,
                ledger=ledger,
                campaigns=campaigns,
                coupons=coupons,
                rule_refs=rule_refs,
                rule_snapshots=rule_store,
                clock=lambda: NOW,
            ),
            workflow_repository=workflow_repository,
        )
