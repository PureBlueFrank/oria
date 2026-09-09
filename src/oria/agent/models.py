"""Strict final-output and termination values for the V0.1 research agent."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, ValidationError, model_validator

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


class AttributionDecisionError(ValueError):
    """Repairable inconsistency with a prior evidence-backed decision audit."""


class AttributionHypothesis(ValueModel):
    hypothesis_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    statement: str = Field(
        min_length=1,
        max_length=1000,
        description="A candidate causal explanation, OR a direct report of a verified fact, "
        "number, trend, or premise-refutation. For report tasks (an exact value, whether an "
        "activity is active, a trend range, or whether a comparison premise holds), state the "
        "verified finding verbatim and do not abstain merely because no causal root cause is "
        "involved. For causal attribution, be explicitly conditional when the mechanism is "
        "unobserved; do not say an event caused an effect and then retract that assertion only "
        "in uncertainty. Observed metric changes are facts; proposed causes are hypotheses.",
    )
    uncertainty: str = Field(min_length=1, max_length=1000)


class AttributionEvidenceRef(ValueModel):
    tool_call_id: str = Field(
        min_length=1,
        max_length=128,
        description="The call_id of the tool call record exactly as issued (for example "
        "call_00_...). Never copy the execution_id inside ToolResult; it is internal "
        "audit metadata, not a citable call ID.",
    )
    tool_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    data_path: str = Field(
        min_length=1,
        max_length=1000,
        description="Exact JSON Pointer relative to ToolResult.data. Prefer a scalar leaf, "
        "e.g. /rows/0/metrics/redemptions. /rows points to the entire array, not a summary.",
    )
    value: JsonValue = Field(
        description="Copy the exact JSON value at data_path, preserving its type. "
        "A numeric leaf must be a number, not a quoted number or sentence. "
        "An object or array pointer requires the complete unchanged object or array. "
        "Never put a paraphrase, computed value, or concatenated summary here.",
    )
    supports: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_json_pointer(self) -> Self:
        if not self.data_path.startswith("/"):
            raise ValueError("attribution evidence data_path must be a JSON Pointer")
        return self


class AttributionCausalAssessment(ValueModel):
    anomalous_conversion_stages: tuple[
        Literal[
            "impression_to_visit",
            "visit_to_enrollment",
            "enrollment_to_confirmation",
            "confirmation_to_redemption",
        ],
        ...,
    ] = Field(
        description="List independently anomalous adjacent conversion rates, not downstream "
        "count changes. Include every observed anomalous stage, even when proposing a common cause."
    )
    shared_mechanism_observed: bool = Field(
        description="True only when evidence directly documents how the same cause acts on "
        "all listed stages. Matching dates, region/category and an activity_type label alone "
        "are NOT mechanism evidence."
    )
    mechanism_evidence: tuple[AttributionEvidenceRef, ...] = Field(
        description="Direct records documenting the cross-stage mechanism, not activity dates "
        "or coincident metric drops. Empty when no such record was returned."
    )


class AttributionCandidateDecision(ValueModel):
    """All observed candidates, including those excluded from the final answer."""

    hypothesis_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    status: Literal["supported", "ruled_out"]
    evidence_indices: tuple[Annotated[int, Field(ge=0)], ...] = Field(
        default=(),
        description="Indices into evidence of the observations supporting this candidate. "
        "For a supported candidate, list the supporting observations. For a ruled_out "
        "candidate, list only observations that initially motivated it (often empty); the "
        "counter-evidence goes in refutation_indices instead, and the two lists must never "
        "share an index.",
    )
    refutation_indices: tuple[Annotated[int, Field(ge=0)], ...] = Field(
        default=(),
        description="Indices into evidence of direct counterevidence ruling this candidate out. "
        "Other segments being stable, missing comparisons, or a stronger alternative do not "
        "refute an observed local signal. Empty for supported candidates.",
    )

    @model_validator(mode="after")
    def validate_refutation(self) -> Self:
        if (self.status == "ruled_out") != bool(self.refutation_indices):
            raise ValueError("ruled_out requires observed refutation; supported forbids refutation")
        if set(self.evidence_indices) & set(self.refutation_indices):
            raise ValueError("support and refutation must be distinct observations")
        return self


class AttributionDecisionAssessment(ValueModel):
    task_kind: Literal["causal", "report"] = Field(
        description="Classify the actual user request, including history. A why question stays "
        "causal even if only descriptive facts are available."
    )
    requested_scope_available: bool = Field(
        description="False when requested dimensions, authorization or data scope are unavailable. "
        "Explaining a tool limitation does not satisfy the original request."
    )
    missing_requirements: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        description="Missing prerequisites to answer the actual request. For causal questions, "
        "stable metrics alone do not explain why. Do not put missing discrimination between "
        "two already supported explanations here: that is conflicting."
    )
    candidates: tuple[AttributionCandidateDecision, ...] = Field(
        description="Inventory every evidence-supported explanation before outcome selection, "
        "including local events and same-segment market signals. Do not drop alternatives "
        "during repair. Report tasks list the directly verified finding."
    )


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
    causal_assessment: AttributionCausalAssessment | None = None
    decision_assessment: AttributionDecisionAssessment | None = None

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        self.validate_decision()
        assessment = self.causal_assessment
        if assessment is not None:
            if any(ref not in self.evidence for ref in assessment.mechanism_evidence):
                raise ValueError("mechanism evidence must also appear in validated evidence")
            if assessment.shared_mechanism_observed != bool(assessment.mechanism_evidence):
                raise ValueError("shared mechanism requires direct mechanism evidence")
            if (
                self.outcome == "attributed"
                and len(set(assessment.anomalous_conversion_stages)) > 1
                and not assessment.shared_mechanism_observed
            ):
                raise ValueError(
                    "multiple anomalous stages without observed shared mechanism "
                    "cannot be attributed to one cause"
                )
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
        if self.outcome == "attributed" and len(self.hypotheses) != 1:
            raise ValueError(
                "attributed requires exactly one retained supported hypothesis; "
                "multiple unresolved supported explanations require conflicting"
            )
        if self.outcome == "conflicting" and (
            self.conclusion is not None or len(self.hypotheses) < 2
        ):
            raise ValueError("conflicting outcome requires multiple hypotheses and no conclusion")
        return self

    def validate_decision(self) -> None:
        audit = self.decision_assessment
        if audit is None:  # Archived outputs remain readable; runtime requires the audit.
            return
        ids = [candidate.hypothesis_id for candidate in audit.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("decision candidate IDs must be unique")
        supported = {
            candidate.hypothesis_id
            for candidate in audit.candidates
            if candidate.status == "supported"
        }
        expected = (
            "insufficient"
            if not audit.requested_scope_available
            else "conflicting"
            if len(supported) > 1
            else "insufficient"
            if audit.missing_requirements or not supported
            else "attributed"
        )
        if self.outcome != expected:
            raise ValueError(
                f"decision rules require {expected}; repair outcome and all dependent fields"
            )
        if self.outcome != "insufficient" and supported != {
            hypothesis.hypothesis_id for hypothesis in self.hypotheses
        }:
            raise ValueError("retain every supported decision candidate in final hypotheses")
        for candidate in audit.candidates:
            for index in (*candidate.evidence_indices, *candidate.refutation_indices):
                if index >= len(self.evidence) or self.evidence[index].value is None:
                    raise ValueError(
                        "decision evidence index must reference a non-null observation"
                    )
            if (
                candidate.status == "supported"
                and self.outcome != "insufficient"
                and not all(
                    candidate.hypothesis_id in self.evidence[index].supports
                    for index in candidate.evidence_indices
                )
            ):
                raise ValueError("candidate support must match evidence.supports")


class AttributionSubmission(AttributionConclusion):
    """Current runtime contract, separate from backwards-readable stored values."""

    causal_assessment: AttributionCausalAssessment
    decision_assessment: AttributionDecisionAssessment


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
    schema = AttributionSubmission.model_json_schema(mode="serialization")
    # Archived v1 values remain readable; newly generated submissions require an audit.
    schema["properties"]["causal_assessment"] = {"$ref": "#/$defs/AttributionCausalAssessment"}
    schema["properties"] = {
        name: schema["properties"][name]
        for name in (
            "decision_assessment",
            "causal_assessment",
            "hypotheses",
            "evidence",
            "schema_version",
            "outcome",
            "conclusion",
            "confidence",
            "confidence_explanation",
            "abstained",
            "requested_data",
        )
    }
    schema["description"] = (
        "Complete decision_assessment and causal_assessment before choosing outcome. "
        "Unavailable requested scope requires insufficient; otherwise multiple supported "
        "candidates "
        "require conflicting even when discrimination is missing. Missing prerequisites or zero "
        "supported candidates require insufficient; exactly one with no missing prerequisites "
        "permits attributed. Never replace a why question with a "
        "descriptive answer or encode a refusal as attributed. "
        "If multiple adjacent conversion "
        "stages are independently anomalous and shared_mechanism_observed=false, attributed "
        "is invalid: preserve the supported independent explanations as conflicting, or "
        "insufficient if the evidence does not support competing explanations. "
        "attributed also covers directly answerable report tasks (an exact value, whether an "
        "activity is active, a trend range, or a premise verification): when the data answers "
        "the question, report the verified fact as the conclusion and do not abstain merely "
        "because no causal root cause is involved; in that case leave "
        "anomalous_conversion_stages empty and set shared_mechanism_observed=false. "
        "Outcome invariants: insufficient requires abstained=true, conclusion=null and "
        "nonempty requested_data. attributed/conflicting require abstained=false, empty "
        "requested_data, nonempty hypotheses and evidence. attributed requires a conclusion "
        "and exactly one retained supported hypothesis; never discard an unresolved "
        "supported explanation just to meet this constraint. "
        "conflicting requires conclusion=null and at least two hypotheses. Hypothesis IDs "
        "must be unique; evidence.supports must reference those IDs, and every hypothesis "
        "must have supporting evidence. Cite exact existing call IDs, tool names, JSON "
        "Pointers relative to ToolResult.data and unchanged observed values. "
        "For observational attribution, state the best-supported explanation rather than "
        "claiming a proven cause or inventing an unobserved mechanism. The conclusion "
        "must preserve the uncertainty stated in hypotheses."
    )
    return ResponseSchema(
        name="attribution_conclusion_v1",
        json_schema=schema,
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
    require_decision: bool = False,
) -> AttributionConclusion:
    model = AttributionSubmission if require_decision else AttributionConclusion
    conclusion = model.model_validate(value)
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


def validate_attribution_repair(
    conclusion: AttributionConclusion,
    drafts: Sequence[dict[str, JsonValue]],
    *,
    tool_results: Mapping[str, Mapping[str, JsonValue]],
) -> None:
    current = conclusion.decision_assessment
    if current is None:
        return
    candidates = {item.hypothesis_id: item for item in current.candidates}
    for draft in drafts:
        try:
            prior = AttributionDecisionAssessment.model_validate(draft.get("decision_assessment"))
            raw_refs = draft.get("evidence", [])
            if not isinstance(raw_refs, list):
                continue
            refs = tuple(AttributionEvidenceRef.model_validate(ref) for ref in raw_refs)
        except ValidationError:
            continue  # An unparseable audit cannot establish candidate obligations.
        if prior.task_kind != current.task_kind:
            raise AttributionDecisionError("repair cannot change the original task_kind")
        if not prior.requested_scope_available and current.requested_scope_available:
            raise AttributionDecisionError("repair cannot make unavailable scope available")
        if not set(prior.missing_requirements).issubset(current.missing_requirements):
            raise AttributionDecisionError(
                "repair cannot erase missing prerequisites without research"
            )
        for candidate in prior.candidates:
            if candidate.status != "supported":
                continue
            if any(index >= len(refs) for index in candidate.evidence_indices):
                continue
            observed_refs = [refs[index] for index in candidate.evidence_indices]
            for ref in observed_refs:
                record = tool_results.get(ref.tool_call_id, {})
                result = record.get("result")
                if not isinstance(result, Mapping) or result.get("ok") is not True:
                    raise ProposalEvidenceError("prior decision used unsuccessful evidence")
                if record.get("tool_name") != ref.tool_name or not _same_json_value(
                    _resolve_json_pointer(result.get("data"), ref.data_path), ref.value
                ):
                    raise ProposalEvidenceError("prior decision evidence does not match ToolResult")
            replacement = candidates.get(candidate.hypothesis_id)
            if replacement is None:
                raise AttributionDecisionError(
                    "repair must retain supported candidates or cite refutation"
                )
            # Evidence indices may move, but support cannot be silently replaced or erased.
            retained = [conclusion.evidence[index] for index in replacement.evidence_indices]
            if not all(
                any(
                    (old.tool_call_id, old.data_path, old.value)
                    == (new.tool_call_id, new.data_path, new.value)
                    for new in retained
                )
                for old in observed_refs
            ):
                raise AttributionDecisionError(
                    "repair must preserve candidate supporting observations"
                )


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
