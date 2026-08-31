"""Tenant-scoped deterministic product catalog query service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from oria.adapters.products import ProductCatalogAdapter, ProductCatalogPolicyBinding
from oria.core.types import AuthorizationContext, AuthorizationRequest, ResourceRef
from oria.domain.product_eligibility import (
    ProductEligibilityCriteria,
    ProductEligibilityPolicy,
)
from oria.domain.repositories import CampaignRepository, CampaignRuleSnapshotRefRepository
from oria.tools.models import QueryEligibleProductsParams, QueryEligibleProductsResult

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.rag.models import CampaignRuleSnapshot


class RuleSnapshotReader(Protocol):
    async def get(self, snapshot_id: str, ctx: Context) -> CampaignRuleSnapshot: ...


class ProductQueryService:
    """Cross-check campaign/snapshot/policy before evaluating a stable catalog page."""

    def __init__(
        self,
        *,
        campaigns: CampaignRepository,
        rule_refs: CampaignRuleSnapshotRefRepository,
        rule_snapshots: RuleSnapshotReader,
        catalog: ProductCatalogAdapter,
        eligibility: ProductEligibilityPolicy,
    ) -> None:
        self._campaigns = campaigns
        self._rule_refs = rule_refs
        self._rule_snapshots = rule_snapshots
        self._catalog = catalog
        self._eligibility = eligibility

    async def query(
        self,
        request: QueryEligibleProductsParams,
        ctx: Context,
    ) -> QueryEligibleProductsResult:
        await self._authorize(request.campaign_id, ctx)
        campaign = await self._campaigns.get(request.campaign_id, ctx)
        if campaign is None or campaign.status != "recruiting":
            raise LookupError("recruiting campaign is unavailable")
        rule_ref = await self._rule_refs.get(campaign.rule_snapshot_ref_id, ctx)
        snapshot = await self._rule_snapshots.get(request.rule_snapshot_id, ctx)
        if (
            rule_ref is None
            or rule_ref.snapshot_id != snapshot.snapshot_id
            or rule_ref.snapshot_hash != snapshot.snapshot_hash
            or snapshot.tenant_id != ctx.tenant_id
        ):
            raise PermissionError("campaign rule snapshot binding does not match")
        criteria = ProductEligibilityCriteria.from_snapshot(snapshot)
        if (
            request.product_circle_policy_ref != criteria.policy_ref
            or request.product_circle_policy_version != criteria.policy_version
        ):
            raise PermissionError("product circle policy binding does not match")
        page = await self._catalog.list_products(
            tenant_id=ctx.tenant_id,
            merchant_ids=request.merchant_ids,
            policy=ProductCatalogPolicyBinding(
                policy_ref=criteria.policy_ref,
                policy_version=criteria.policy_version,
            ),
            cursor=request.cursor,
            limit=request.limit,
        )
        requested_merchants = frozenset(request.merchant_ids)
        if any(product.merchant_id not in requested_merchants for product in page.products):
            raise PermissionError("product catalog returned an out-of-scope merchant")
        eligible, exclusion_counts = self._eligibility.evaluate_page(page.products, criteria)
        return QueryEligibleProductsResult(
            campaign_id=campaign.campaign_id,
            rule_snapshot_id=snapshot.snapshot_id,
            product_circle_policy_ref=criteria.policy_ref,
            product_circle_policy_version=criteria.policy_version,
            catalog_snapshot_id=page.catalog_snapshot_id,
            evaluated_count=len(page.products),
            eligible_count=len(eligible),
            excluded_count=len(page.products) - len(eligible),
            products=eligible,
            exclusion_reason_counts=exclusion_counts,
            next_cursor=page.next_cursor,
        )

    @staticmethod
    async def _authorize(campaign_id: str, ctx: Context) -> None:
        decision = await ctx.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action="product:read",
                resource=ResourceRef(
                    resource_type="campaign",
                    resource_id=campaign_id,
                    tenant_id=ctx.tenant_id,
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            ),
            ctx,
        )
        if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
            raise PermissionError("product catalog read is not authorized")
