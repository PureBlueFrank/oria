"""Typed domain services and the fixed container exposed through Context."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    ResourceRef,
    ServiceHealth,
)
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.launch import CampaignLaunchService
from oria.domain.models import (
    CampaignRuleSet,
    EligibilityCriteria,
    EligibilityReason,
    EligibleMerchantSet,
    Merchant,
    MerchantRecord,
)
from oria.domain.repositories import MerchantRepository

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.rag.models import CampaignRuleSnapshot


class CampaignRuleService(Protocol):
    async def get_rule_set(self, rule_set_id: str, ctx: Context) -> CampaignRuleSet: ...

    async def health(self, ctx: Context) -> ServiceHealth: ...


class MerchantService(Protocol):
    async def eligible_merchants(
        self,
        rule_set_id: str,
        limit: int,
        ctx: Context,
    ) -> EligibleMerchantSet: ...

    async def eligible_merchants_for_snapshot(
        self,
        snapshot: CampaignRuleSnapshot,
        limit: int,
        ctx: Context,
    ) -> EligibleMerchantSet: ...

    async def health(self, ctx: Context) -> ServiceHealth: ...


class PackageCampaignRuleService:
    __slots__ = ("__rules",)

    def __init__(self, rules: CampaignRuleSet) -> None:
        self.__rules = rules

    async def get_rule_set(self, rule_set_id: str, ctx: Context) -> CampaignRuleSet:
        if rule_set_id != self.__rules.rule_set_id or ctx.tenant_id != self.__rules.tenant_id:
            raise LookupError("campaign rule set is unavailable")
        await _authorize("rule:read", "campaign_rule", rule_set_id, ctx)
        return self.__rules

    async def health(self, ctx: Context) -> ServiceHealth:
        await _authorize("rule:read", "campaign_rule", self.__rules.rule_set_id, ctx)
        return ServiceHealth(ready=True, detail=f"rules:{self.__rules.version}")


class DefaultMerchantService:
    __slots__ = ("__eligibility", "__repository", "__rules")

    def __init__(
        self,
        repository: MerchantRepository,
        eligibility: EligibilityPolicy,
        rules: CampaignRuleService,
    ) -> None:
        self.__repository = repository
        self.__eligibility = eligibility
        self.__rules = rules

    async def eligible_merchants(
        self,
        rule_set_id: str,
        limit: int,
        ctx: Context,
    ) -> EligibleMerchantSet:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rules = await self.__rules.get_rule_set(rule_set_id, ctx)
        if rules.tenant_id != ctx.tenant_id:
            raise PermissionError("domain read is not authorized")
        await _authorize("merchant:read", "merchant_catalog", "eligible", ctx)
        records = await self.__repository.list_for_eligibility(ctx)
        if any(record.tenant_id != ctx.tenant_id for record in records):
            raise PermissionError("domain read is not authorized")
        return self._evaluate_records(
            records,
            rules.internal_eligibility_criteria(),
            limit,
        )

    async def eligible_merchants_for_snapshot(
        self,
        snapshot: CampaignRuleSnapshot,
        limit: int,
        ctx: Context,
    ) -> EligibleMerchantSet:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if (
            snapshot.tenant_id != ctx.tenant_id
            or snapshot.recompute_hash() != snapshot.snapshot_hash
        ):
            raise PermissionError("domain read is not authorized")
        scope = snapshot.recruitment_scope
        criteria = EligibilityCriteria(
            rule_set_id=snapshot.snapshot_id,
            rule_version=snapshot.snapshot_hash,
            categories=scope.categories,
            cities=scope.cities,
            enrollment_systems=scope.enrollment_systems,
            allowlist_merchant_ids=tuple(sorted(scope.internal_allowlist())),
            denylist_merchant_ids=tuple(sorted(scope.internal_denylist())),
            sales_org_scope=tuple(sorted(scope.internal_sales_org_scope())),
        )
        await _authorize("merchant:read", "merchant_catalog", "eligible", ctx)
        records = await self.__repository.list_for_eligibility(ctx)
        if any(record.tenant_id != ctx.tenant_id for record in records):
            raise PermissionError("domain read is not authorized")
        return self._evaluate_records(records, criteria, limit)

    def _evaluate_records(
        self,
        records: tuple[MerchantRecord, ...],
        criteria: EligibilityCriteria,
        limit: int,
    ) -> EligibleMerchantSet:
        candidates: list[Merchant] = []
        exclusion_counts: Counter[EligibilityReason] = Counter()
        for record in records:
            decision = self.__eligibility.evaluate(record, criteria)
            if decision.eligible:
                candidates.append(
                    Merchant(
                        tenant_id=record.tenant_id,
                        merchant_id=record.merchant_id,
                        version=record.version,
                        display_name=record.display_name,
                        categories=record.categories,
                        cities=record.cities,
                        enrollment_systems=record.enrollment_systems,
                    )
                )
                continue
            exclusion_counts.update(decision.reason_codes)
        return EligibleMerchantSet(
            rule_set_id=criteria.rule_set_id,
            rule_version=criteria.rule_version,
            evaluated_count=len(records),
            eligible_count=len(candidates),
            merchants=tuple(candidates[:limit]),
            exclusion_reason_counts=dict(sorted(exclusion_counts.items())),
        )

    async def health(self, ctx: Context) -> ServiceHealth:
        await _authorize("merchant:read", "merchant_catalog", "health", ctx)
        await self.__repository.list_for_eligibility(ctx)
        return ServiceHealth(ready=True, detail="merchant repository ready")


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
            context=AuthorizationContext(correlation_id=ctx.run_id),
        ),
        ctx,
    )
    if not decision.allow:
        raise PermissionError("domain read is not authorized")


@dataclass(frozen=True, slots=True)
class DomainServiceRegistry:
    """Factory-owned typed services; Repository and DB resources are not exposed."""

    campaign_rules: CampaignRuleService
    merchants: MerchantService
    campaign_launch: CampaignLaunchService
