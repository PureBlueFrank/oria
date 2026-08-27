"""Typed domain services and the fixed container exposed through Context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    ResourceRef,
    ServiceHealth,
)
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.models import CampaignRuleSet, EligibleMerchantSet
from oria.domain.repositories import MerchantRepository

if TYPE_CHECKING:
    from oria.core.context import Context


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
        await _authorize("merchant:read", "merchant_catalog", "eligible", ctx)
        records = await self.__repository.list_for_eligibility(ctx)
        candidates = self.__eligibility.eligible_merchants(
            records, rules.internal_eligibility_criteria()
        )
        return EligibleMerchantSet(
            rule_set_id=rules.rule_set_id,
            rule_version=rules.version,
            evaluated_count=len(records),
            merchants=candidates[:limit],
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
