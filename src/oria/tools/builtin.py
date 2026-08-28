"""The two read-only, model-visible tools used by the V0.1 hero flow."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.tools.models import (
    MerchantCandidate,
    PublicCampaignRules,
    PublicRecruitmentScope,
    QueryMerchantsParams,
    QueryMerchantsResult,
    SearchCampaignRulesParams,
    SearchCampaignRulesResult,
)

if TYPE_CHECKING:
    from oria.core.context import Context
    from oria.core.protocols import Retriever
    from oria.domain.services import MerchantService
    from oria.rag.snapshots import LocalRuleSnapshotStore


def _execution_id() -> str:
    return f"tool_{uuid.uuid4().hex}"


_RESTRICTED_EVIDENCE_PREFIXES = (
    "recruitment_scope.allowlist_merchant_ids",
    "recruitment_scope.denylist_merchant_ids",
    "recruitment_scope.sales_org_scope",
)


class SearchCampaignRulesTool:
    name = "search_campaign_rules"
    schema_version = 1
    description = "Search effective campaign rules and return a redacted snapshot with citations."
    json_schema: dict[str, Any] = SearchCampaignRulesParams.model_json_schema()
    result_schema: dict[str, Any] = SearchCampaignRulesResult.model_json_schema(
        mode="serialization"
    )
    policy = ToolPolicy(
        risk_level="low",
        side_effect=False,
        timeout_seconds=15,
        retry_policy=RetryPolicy(max_attempts=1),
        required_action="rule:read",
        resource_type="campaign_rule",
        approval_mode="none",
    )

    def __init__(self, retriever: Retriever, snapshots: LocalRuleSnapshotStore) -> None:
        self._retriever = retriever
        self._snapshots = snapshots

    def validate_params(self, params: dict[str, Any]) -> None:
        SearchCampaignRulesParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = SearchCampaignRulesParams.model_validate(params)
        docs = await self._retriever.retrieve(request.intent, ctx, k=50)
        resolution = await self._snapshots.resolve(
            docs,
            effective_at=request.effective_at,
            ctx=ctx,
        )
        if resolution.snapshot is None:
            data = SearchCampaignRulesResult(
                effective_at=request.effective_at,
                unresolved_items=resolution.unresolved_items,
            )
        else:
            snapshot = resolution.snapshot
            scope = snapshot.recruitment_scope
            data = SearchCampaignRulesResult(
                rule_snapshot_id=snapshot.snapshot_id,
                snapshot_hash=snapshot.snapshot_hash,
                effective_at=snapshot.effective_at,
                rules=PublicCampaignRules(
                    basic=snapshot.basic,
                    recruitment_scope=PublicRecruitmentScope(
                        categories=scope.categories,
                        cities=scope.cities,
                        enrollment_systems=scope.enrollment_systems,
                    ),
                    enrollment_policy=snapshot.enrollment_policy,
                    benefit_policy=snapshot.benefit_policy,
                    confirmation_policy=snapshot.confirmation_policy,
                    merchant_material=snapshot.merchant_material,
                ),
                field_evidence={
                    path: evidence.as_citation()
                    for path, evidence in sorted(snapshot.field_evidence.items())
                    if not path.startswith(_RESTRICTED_EVIDENCE_PREFIXES)
                },
            )
        return ToolResult(
            ok=True,
            data=data.model_dump(mode="json"),
            execution_id=_execution_id(),
            trust_level="trusted_internal",
            provenance="oria://tool/search_campaign_rules/v1",
            data_classification="restricted_derivative",
        )


class QueryMerchantsTool:
    name = "query_merchants"
    schema_version = 1
    description = "Filter merchants by a verified rule snapshot and return redacted candidates."
    json_schema: dict[str, Any] = QueryMerchantsParams.model_json_schema()
    result_schema: dict[str, Any] = QueryMerchantsResult.model_json_schema(mode="serialization")
    policy = ToolPolicy(
        risk_level="low",
        side_effect=False,
        timeout_seconds=15,
        retry_policy=RetryPolicy(max_attempts=1),
        required_action="merchant:read",
        resource_type="merchant_catalog",
        approval_mode="none",
    )

    def __init__(self, snapshots: LocalRuleSnapshotStore, merchants: MerchantService) -> None:
        self._snapshots = snapshots
        self._merchants = merchants

    def validate_params(self, params: dict[str, Any]) -> None:
        QueryMerchantsParams.model_validate(params)

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        request = QueryMerchantsParams.model_validate(params)
        snapshot = await self._snapshots.get(request.rule_snapshot_id, ctx)
        result = await self._merchants.eligible_merchants_for_snapshot(
            snapshot,
            request.limit,
            ctx,
        )
        candidates = tuple(
            MerchantCandidate(
                merchant_id=merchant.merchant_id,
                version=merchant.version,
                display_name=merchant.display_name,
                categories=merchant.categories,
                cities=merchant.cities,
                enrollment_systems=merchant.enrollment_systems,
            )
            for merchant in result.merchants
        )
        data = QueryMerchantsResult(
            rule_snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            evaluated_count=result.evaluated_count,
            eligible_count=result.eligible_count,
            returned_count=len(candidates),
            excluded_count=result.evaluated_count - result.eligible_count,
            candidates=candidates,
            exclusion_reason_counts={
                reason: count for reason, count in result.exclusion_reason_counts.items()
            },
        )
        return ToolResult(
            ok=True,
            data=data.model_dump(mode="json"),
            execution_id=_execution_id(),
            trust_level="trusted_internal",
            provenance="oria://tool/query_merchants/v1",
            data_classification="internal",
        )
