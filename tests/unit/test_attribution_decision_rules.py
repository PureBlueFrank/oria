"""Decision contracts use synthetic observations, never frozen evaluation answers."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from oria.agent.models import validate_attribution_conclusion

pytestmark = pytest.mark.unit


def submission():
    evidence = [
        {
            "tool_call_id": "observed",
            "tool_name": "query_funnel",
            "data_path": "/value",
            "value": 12,
            "supports": ["local"],
        },
        {
            "tool_call_id": "market",
            "tool_name": "query_market_overview",
            "data_path": "/value",
            "value": -0.2,
            "supports": [],
        },
    ]
    return {
        "outcome": "attributed",
        "conclusion": "A bounded local explanation.",
        "hypotheses": [
            {
                "hypothesis_id": "local",
                "statement": "Local signal.",
                "uncertainty": "Observational.",
            }
        ],
        "evidence": evidence,
        "confidence": 0.5,
        "confidence_explanation": "Uncalibrated.",
        "abstained": False,
        "requested_data": [],
        "causal_assessment": {
            "anomalous_conversion_stages": [],
            "shared_mechanism_observed": False,
            "mechanism_evidence": [],
        },
        "decision_assessment": {
            "task_kind": "causal",
            "requested_scope_available": True,
            "missing_requirements": [],
            "candidates": [
                {
                    "hypothesis_id": "local",
                    "status": "supported",
                    "evidence_indices": [0],
                    "refutation_indices": [],
                }
            ],
        },
    }


def records():
    return {
        "observed": {"tool_name": "query_funnel", "result": {"ok": True, "data": {"value": 12}}},
        "market": {
            "tool_name": "query_market_overview",
            "result": {"ok": True, "data": {"value": -0.2}},
        },
    }


def test_supported_competitor_cannot_be_deleted_from_final_hypotheses():
    value = submission()
    value["decision_assessment"]["candidates"].append(
        {
            "hypothesis_id": "market",
            "status": "supported",
            "evidence_indices": [1],
            "refutation_indices": [],
        }
    )
    with pytest.raises(ValidationError, match="conflicting"):
        validate_attribution_conclusion(value, tool_results=records())


@pytest.mark.parametrize("gap", ["scope", "evidence"])
def test_unavailable_request_cannot_be_a_successful_report(gap):
    value = submission()
    value["decision_assessment"]["task_kind"] = "report"
    if gap == "scope":
        value["decision_assessment"]["requested_scope_available"] = False
    else:
        value["decision_assessment"]["missing_requirements"] = ["Evidence answering why."]
    with pytest.raises(ValidationError, match="insufficient"):
        validate_attribution_conclusion(value, tool_results=records())


def test_competitor_needs_observed_refutation_to_be_excluded():
    value = submission()
    value["decision_assessment"]["candidates"].append(
        {
            "hypothesis_id": "market",
            "status": "ruled_out",
            "evidence_indices": [1],
            "refutation_indices": [],
        }
    )
    with pytest.raises(ValidationError, match="refutation"):
        validate_attribution_conclusion(value, tool_results=records())


def test_current_submission_requires_audit_but_archive_remains_readable():
    value = submission()
    del value["decision_assessment"]
    validate_attribution_conclusion(value, tool_results=records())
    with pytest.raises(ValidationError, match="decision_assessment"):
        validate_attribution_conclusion(value, tool_results=records(), require_decision=True)


def test_report_and_insufficient_remain_valid():
    value = submission()
    value["decision_assessment"]["task_kind"] = "report"
    assert validate_attribution_conclusion(value, tool_results=records()).outcome == "attributed"
    missing = deepcopy(value)
    missing.update(
        outcome="insufficient",
        conclusion=None,
        abstained=True,
        requested_data=["Authorized dimension-level data."],
    )
    missing["decision_assessment"].update(
        requested_scope_available=False, candidates=[], missing_requirements=["Dimension data."]
    )
    assert validate_attribution_conclusion(missing, tool_results=records()).abstained


def test_repair_cannot_drop_previously_supported_competitor():
    from oria.agent.models import AttributionDecisionError, validate_attribution_repair

    draft = submission()
    draft["decision_assessment"]["candidates"].append(
        {
            "hypothesis_id": "market",
            "status": "supported",
            "evidence_indices": [1],
            "refutation_indices": [],
        }
    )
    repaired = validate_attribution_conclusion(submission(), tool_results=records())
    with pytest.raises(AttributionDecisionError, match="retain supported"):
        validate_attribution_repair(repaired, [draft], tool_results=records())


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_scope_available", False),
        ("missing_requirements", ["Missing original evidence."]),
        ("task_kind", "report"),
    ],
)
def test_repair_cannot_erase_prior_scope_or_question_constraints(field, value):
    from oria.agent.models import AttributionDecisionError, validate_attribution_repair

    draft = submission()
    draft["decision_assessment"][field] = value
    repaired = validate_attribution_conclusion(submission(), tool_results=records())
    with pytest.raises(AttributionDecisionError, match="repair cannot"):
        validate_attribution_repair(repaired, [draft], tool_results=records())


@pytest.mark.parametrize("index", [-1, 5])
def test_invalid_candidate_reference_cannot_bypass_validation(index):
    value = submission()
    value["decision_assessment"]["candidates"][0]["evidence_indices"] = [index]
    with pytest.raises(ValidationError):
        validate_attribution_conclusion(value, tool_results=records())


def test_null_comparison_is_not_support_or_counterevidence():
    value = submission()
    value["evidence"][0]["value"] = None
    observed = records()
    observed["observed"]["result"]["data"]["value"] = None
    with pytest.raises(ValidationError, match="non-null"):
        validate_attribution_conclusion(value, tool_results=observed)


def test_missing_discrimination_cannot_downgrade_supported_conflict_to_insufficient():
    value = submission()
    value.update(
        outcome="insufficient",
        conclusion=None,
        abstained=True,
        requested_data=["Discriminating evidence."],
    )
    value["decision_assessment"]["missing_requirements"] = ["Discriminating evidence."]
    value["decision_assessment"]["candidates"].append(
        {
            "hypothesis_id": "market",
            "status": "supported",
            "evidence_indices": [1],
            "refutation_indices": [],
        }
    )
    with pytest.raises(ValidationError, match="conflicting"):
        validate_attribution_conclusion(value, tool_results=records())


def test_supported_conflict_is_accepted_and_observed_refutation_can_resolve_it():
    from oria.agent.models import validate_attribution_repair

    value = submission()
    value.update(outcome="conflicting", conclusion=None)
    value["hypotheses"].append(
        {
            "hypothesis_id": "market",
            "statement": "Independent market signal.",
            "uncertainty": "Cannot separate contributions.",
        }
    )
    value["evidence"][1]["supports"] = ["market"]
    value["decision_assessment"]["candidates"].append(
        {
            "hypothesis_id": "market",
            "status": "supported",
            "evidence_indices": [1],
            "refutation_indices": [],
        }
    )
    assert validate_attribution_conclusion(value, tool_results=records()).outcome == "conflicting"
    resolved = deepcopy(value)
    resolved.update(outcome="attributed", conclusion="Local signal with observed counterevidence.")
    resolved["hypotheses"] = resolved["hypotheses"][:1]
    resolved["evidence"][1]["supports"] = []
    resolved["evidence"].append(
        {
            "tool_call_id": "counter",
            "tool_name": "query_market_overview",
            "data_path": "/value",
            "value": "Independent counter-observation.",
            "supports": [],
        }
    )
    resolved["decision_assessment"]["candidates"][1].update(
        status="ruled_out", refutation_indices=[2]
    )
    observed = records()
    observed["counter"] = {
        "tool_name": "query_market_overview",
        "result": {"ok": True, "data": {"value": "Independent counter-observation."}},
    }
    answer = validate_attribution_conclusion(resolved, tool_results=observed)
    validate_attribution_repair(answer, [value], tool_results=observed)
    assert answer.outcome == "attributed"
