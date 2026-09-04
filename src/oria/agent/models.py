"""Strict final-output and termination values for the V0.1 research agent."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from oria.core.types import CitationBlock, JsonValue, ResponseSchema, ValueModel
from oria.domain.models import BenefitTierRule
from oria.tools.models import (
    PublicCampaignRules,
    QueryMerchantsResult,
    SearchCampaignRulesResult,
)


class CampaignPreview(ValueModel):
    template_ref: str
    campaign_type: str
    campaign_window: str
    enrollment_window: str
    title: str
    hero_image_ref: str


class CouponBatchPreview(ValueModel):
    currency: str
    budget_cap: Decimal = Field(gt=0)
    tier_rules: tuple[BenefitTierRule, ...] = Field(min_length=1)


class MerchantRecommendation(ValueModel):
    merchant_id: str
    rank: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class CampaignProposal(ValueModel):
    schema_version: Literal[1] = 1
    rule_snapshot_id: str | None = Field(default=None, pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    snapshot_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    rules: PublicCampaignRules | None = None
    campaign_preview: CampaignPreview | None = None
    coupon_batch_preview: CouponBatchPreview | None = None
    recommended_merchants: tuple[MerchantRecommendation, ...] = ()
    field_evidence: dict[str, CitationBlock] = Field(default_factory=dict)
    unresolved_items: tuple[str, ...] = ()
    abstained: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.abstained:
            if self.recommended_merchants or not self.unresolved_items:
                raise ValueError("abstained proposals require unresolved items and no merchants")
            return self
        if any(
            item is None
            for item in (
                self.rule_snapshot_id,
                self.snapshot_hash,
                self.rules,
                self.campaign_preview,
                self.coupon_batch_preview,
            )
        ):
            raise ValueError("non-abstained proposals require complete rule and preview fields")
        if not self.recommended_merchants or self.unresolved_items or not self.field_evidence:
            raise ValueError("non-abstained proposals require merchants and evidence")
        ids = tuple(item.merchant_id for item in self.recommended_merchants)
        ranks = tuple(item.rank for item in self.recommended_merchants)
        if len(set(ids)) != len(ids) or ranks != tuple(range(1, len(ranks) + 1)):
            raise ValueError("merchant recommendations require unique IDs and contiguous ranks")
        return self


class CampaignProposalDraft(ValueModel):
    """LLM-owned soft ranking only; trusted rule fields are assembled locally."""

    schema_version: Literal[1] = 1
    recommended_merchants: tuple[MerchantRecommendation, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    abstained: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.abstained:
            if self.recommended_merchants or not self.unresolved_items:
                raise ValueError("abstained drafts require unresolved items and no merchants")
            return self
        if not self.recommended_merchants or self.unresolved_items:
            raise ValueError("non-abstained drafts require merchants and no unresolved items")
        ids = tuple(item.merchant_id for item in self.recommended_merchants)
        ranks = tuple(item.rank for item in self.recommended_merchants)
        if len(set(ids)) != len(ids) or ranks != tuple(range(1, len(ranks) + 1)):
            raise ValueError("merchant recommendations require unique IDs and contiguous ranks")
        return self


class AgentTermination(ValueModel):
    status: Literal["failed", "waiting"]
    reason: str
    limits: dict[str, JsonValue]
    observed_usage: dict[str, JsonValue]
    last_safe_evidence_refs: tuple[str, ...] = ()


class ProposalEvidenceError(ValueError):
    """A non-repairable mismatch against trusted tools or citations."""


class AttributionHypothesis(ValueModel):
    hypothesis_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    statement: str = Field(min_length=1, max_length=1000)
    uncertainty: str = Field(min_length=1, max_length=1000)


class AttributionEvidenceRef(ValueModel):
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    data_path: str = Field(min_length=1, max_length=1000)
    value: JsonValue
    supports: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_json_pointer(self) -> Self:
        if not self.data_path.startswith("/"):
            raise ValueError("attribution evidence data_path must be a JSON Pointer")
        return self


class AttributionConclusion(ValueModel):
    """Evidence-grounded three-state output for Scenario B."""

    schema_version: Literal[1] = 1
    outcome: Literal["attributed", "conflicting", "insufficient"]
    conclusion: str | None = Field(default=None, min_length=1, max_length=2000)
    hypotheses: tuple[AttributionHypothesis, ...] = ()
    evidence: tuple[AttributionEvidenceRef, ...] = ()
    confidence: float = Field(ge=0, le=1)
    confidence_explanation: str = Field(min_length=1, max_length=1000)
    abstained: bool
    requested_data: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        hypothesis_ids = tuple(item.hypothesis_id for item in self.hypotheses)
        if len(set(hypothesis_ids)) != len(hypothesis_ids):
            raise ValueError("attribution hypothesis IDs must be unique")
        supported_ids = {item for ref in self.evidence for item in ref.supports}
        if not supported_ids.issubset(hypothesis_ids):
            raise ValueError("attribution evidence references an unknown hypothesis")

        if self.outcome == "insufficient":
            if not self.abstained or self.conclusion is not None or not self.requested_data:
                raise ValueError(
                    "insufficient attribution must abstain without a conclusion and request data"
                )
            return self

        if self.abstained or self.requested_data:
            raise ValueError("attributed or conflicting outcomes cannot abstain or request data")
        if not self.hypotheses or not self.evidence:
            raise ValueError("attributed or conflicting outcomes require hypotheses and evidence")
        if set(hypothesis_ids).difference(supported_ids):
            raise ValueError("every attribution hypothesis must have supporting evidence")
        if self.outcome == "attributed" and self.conclusion is None:
            raise ValueError("attributed outcome requires a conclusion")
        if self.outcome == "conflicting" and (
            self.conclusion is not None or len(self.hypotheses) < 2
        ):
            raise ValueError("conflicting outcome requires multiple hypotheses and no conclusion")
        return self


def campaign_proposal_schema() -> ResponseSchema:
    return ResponseSchema(
        name="campaign_proposal_v1",
        json_schema=CampaignProposal.model_json_schema(mode="serialization"),
    )


def campaign_proposal_draft_schema() -> ResponseSchema:
    return ResponseSchema(
        name="campaign_proposal_draft_v1",
        json_schema=CampaignProposalDraft.model_json_schema(mode="serialization"),
    )


def attribution_conclusion_schema() -> ResponseSchema:
    return ResponseSchema(
        name="attribution_conclusion_v1",
        json_schema=AttributionConclusion.model_json_schema(mode="serialization"),
    )


def _resolve_json_pointer(value: JsonValue, pointer: str) -> JsonValue:
    current: Any = value
    for encoded_part in pointer.removeprefix("/").split("/"):
        if "~" in encoded_part.replace("~1", "").replace("~0", ""):
            raise ProposalEvidenceError("evidence JSON Pointer contains an invalid escape")
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if part not in current:
                raise ProposalEvidenceError("evidence JSON Pointer does not exist")
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if not part.isdigit():
                raise ProposalEvidenceError("evidence JSON Pointer array index is invalid")
            index = int(part)
            if index >= len(current):
                raise ProposalEvidenceError("evidence JSON Pointer does not exist")
            current = current[index]
        else:
            raise ProposalEvidenceError("evidence JSON Pointer does not exist")
    return cast(JsonValue, current)


def _same_json_value(left: JsonValue, right: JsonValue) -> bool:
    return json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) == json.dumps(right, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_attribution_conclusion(
    value: dict[str, JsonValue],
    *,
    tool_results: Mapping[str, Mapping[str, JsonValue]],
) -> AttributionConclusion:
    conclusion = AttributionConclusion.model_validate(value)
    seen_refs: set[tuple[str, str]] = set()
    for evidence in conclusion.evidence:
        identity = (evidence.tool_call_id, evidence.data_path)
        if identity in seen_refs:
            raise ProposalEvidenceError("attribution evidence references must be unique")
        seen_refs.add(identity)
        record = tool_results.get(evidence.tool_call_id)
        if record is None or record.get("tool_name") != evidence.tool_name:
            raise ProposalEvidenceError("attribution evidence tool call does not exist")
        result = record.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise ProposalEvidenceError("attribution evidence requires a successful ToolResult")
        data = result.get("data")
        observed = _resolve_json_pointer(data, evidence.data_path)
        if not _same_json_value(observed, evidence.value):
            raise ProposalEvidenceError("attribution evidence value does not match ToolResult")
    return conclusion


def validate_campaign_proposal(
    value: dict[str, JsonValue],
    *,
    rules: SearchCampaignRulesResult | None,
    merchants: QueryMerchantsResult | None,
) -> CampaignProposal:
    proposal = CampaignProposal.model_validate(value)
    if proposal.abstained:
        return proposal
    if rules is None or merchants is None or rules.rules is None:
        raise ProposalEvidenceError("proposal requires trusted rule and merchant evidence")
    if (
        proposal.rule_snapshot_id != rules.rule_snapshot_id
        or proposal.snapshot_hash != rules.snapshot_hash
        or proposal.rules != rules.rules
        or proposal.field_evidence != rules.field_evidence
        or merchants.rule_snapshot_id != rules.rule_snapshot_id
        or merchants.snapshot_hash != rules.snapshot_hash
    ):
        raise ProposalEvidenceError("proposal rule snapshot or citations do not match evidence")
    expected_campaign = CampaignPreview(
        template_ref=rules.rules.basic.template_ref,
        campaign_type=rules.rules.basic.campaign_type,
        campaign_window=rules.rules.basic.campaign_window,
        enrollment_window=rules.rules.basic.enrollment_window,
        title=rules.rules.merchant_material.title,
        hero_image_ref=rules.rules.merchant_material.hero_image_ref,
    )
    expected_coupon = CouponBatchPreview(
        currency=rules.rules.benefit_policy.currency,
        budget_cap=rules.rules.benefit_policy.budget_cap,
        tier_rules=rules.rules.benefit_policy.tier_rules,
    )
    if (
        proposal.campaign_preview != expected_campaign
        or proposal.coupon_batch_preview != expected_coupon
    ):
        raise ProposalEvidenceError("proposal previews do not match rule evidence")
    candidates = {item.merchant_id for item in merchants.candidates}
    proposed = {item.merchant_id for item in proposal.recommended_merchants}
    if not proposed.issubset(candidates):
        raise ProposalEvidenceError(
            "proposal contains a merchant outside the eligible candidate set"
        )
    return proposal


def finalize_campaign_proposal_draft(
    value: dict[str, JsonValue],
    *,
    rules: SearchCampaignRulesResult | None,
    merchants: QueryMerchantsResult | None,
    max_candidates: int,
) -> CampaignProposal:
    unexpected = set(value).difference(CampaignProposalDraft.model_fields)
    if unexpected:
        raise ProposalEvidenceError("model draft contains authoritative or unknown fields")
    draft = CampaignProposalDraft.model_validate(value)
    if draft.abstained:
        return CampaignProposal(
            unresolved_items=draft.unresolved_items,
            abstained=True,
        )
    if rules is None or merchants is None or rules.rules is None:
        raise ProposalEvidenceError("proposal requires trusted rule and merchant evidence")
    if merchants.returned_count > max_candidates:
        raise ProposalEvidenceError("merchant evidence exceeds the requested candidate limit")
    if len(draft.recommended_merchants) > max_candidates:
        raise ProposalEvidenceError("proposal exceeds the requested candidate limit")
    proposal = CampaignProposal(
        rule_snapshot_id=rules.rule_snapshot_id,
        snapshot_hash=rules.snapshot_hash,
        rules=rules.rules,
        campaign_preview=CampaignPreview(
            template_ref=rules.rules.basic.template_ref,
            campaign_type=rules.rules.basic.campaign_type,
            campaign_window=rules.rules.basic.campaign_window,
            enrollment_window=rules.rules.basic.enrollment_window,
            title=rules.rules.merchant_material.title,
            hero_image_ref=rules.rules.merchant_material.hero_image_ref,
        ),
        coupon_batch_preview=CouponBatchPreview(
            currency=rules.rules.benefit_policy.currency,
            budget_cap=rules.rules.benefit_policy.budget_cap,
            tier_rules=rules.rules.benefit_policy.tier_rules,
        ),
        recommended_merchants=draft.recommended_merchants,
        field_evidence=dict(rules.field_evidence),
    )
    return validate_campaign_proposal(
        proposal.model_dump(mode="json"),
        rules=rules,
        merchants=merchants,
    )
