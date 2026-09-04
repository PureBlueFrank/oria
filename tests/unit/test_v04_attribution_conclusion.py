"""V0.4-T03 attribution output and evidence contracts."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from oria.agent.models import (
    ProposalEvidenceError,
    attribution_conclusion_schema,
    validate_attribution_conclusion,
)

pytestmark = pytest.mark.unit


def _tool_results() -> dict[str, dict[str, Any]]:
    return {
        "call-funnel": {
            "tool_name": "query_funnel",
            "arguments": {},
            "result": {
                "ok": True,
                "data": {"rows": [{"category": "segment-a", "visits": 120, "redemptions": 18}]},
            },
        },
        "call-market": {
            "tool_name": "query_market_overview",
            "arguments": {},
            "result": {
                "ok": True,
                "data": {"segments": [{"category": "segment-a", "change_rate": 0.25}]},
            },
        },
    }


def _evidence(
    call_id: str,
    tool_name: str,
    path: str,
    value: object,
    supports: list[str],
) -> dict[str, object]:
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "data_path": path,
        "value": value,
        "supports": supports,
    }


def test_attributed_conclusion_validates_exact_tool_result_values() -> None:
    value = {
        "schema_version": 1,
        "outcome": "attributed",
        "conclusion": "Observed conversion loss is concentrated in segment-a.",
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "The loss is segment-specific.",
                "uncertainty": "Only the requested period is observed.",
            }
        ],
        "evidence": [
            _evidence(
                "call-funnel",
                "query_funnel",
                "/rows/0/redemptions",
                18,
                ["h1"],
            )
        ],
        "confidence": 0.77,
        "confidence_explanation": "Explanatory and not calibrated as a gate.",
        "abstained": False,
        "requested_data": [],
    }

    result = validate_attribution_conclusion(value, tool_results=_tool_results())

    assert result.outcome == "attributed"
    assert attribution_conclusion_schema().name == "attribution_conclusion_v1"


def test_conflicting_and_insufficient_shapes_preserve_uncertainty() -> None:
    conflicting = {
        "schema_version": 1,
        "outcome": "conflicting",
        "conclusion": None,
        "hypotheses": [
            {"hypothesis_id": "h1", "statement": "Local loss.", "uncertainty": "A."},
            {"hypothesis_id": "h2", "statement": "Market gain.", "uncertainty": "B."},
        ],
        "evidence": [
            _evidence("call-funnel", "query_funnel", "/rows/0/visits", 120, ["h1"]),
            _evidence(
                "call-market",
                "query_market_overview",
                "/segments/0/change_rate",
                0.25,
                ["h2"],
            ),
        ],
        "confidence": 0.4,
        "confidence_explanation": "Signals conflict.",
        "abstained": False,
        "requested_data": [],
    }
    insufficient = {
        "schema_version": 1,
        "outcome": "insufficient",
        "conclusion": None,
        "hypotheses": [],
        "evidence": [],
        "confidence": 0.1,
        "confidence_explanation": "No decisive observation is available.",
        "abstained": True,
        "requested_data": ["A longer comparison period."],
    }

    assert (
        validate_attribution_conclusion(conflicting, tool_results=_tool_results()).outcome
        == "conflicting"
    )
    assert (
        validate_attribution_conclusion(insufficient, tool_results=_tool_results()).abstained
        is True
    )

    with pytest.raises(ValidationError, match="multiple hypotheses and no conclusion"):
        validate_attribution_conclusion(
            {**conflicting, "conclusion": "Forced unique conclusion."},
            tool_results=_tool_results(),
        )
    with pytest.raises(ValidationError, match="must abstain without a conclusion"):
        validate_attribution_conclusion(
            {**insufficient, "abstained": False}, tool_results=_tool_results()
        )


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence("missing", "query_funnel", "/rows/0/visits", 120, ["h1"]),
        _evidence("call-funnel", "drill_down", "/rows/0/visits", 120, ["h1"]),
        _evidence("call-funnel", "query_funnel", "/rows/0/missing", 120, ["h1"]),
        _evidence("call-funnel", "query_funnel", "/rows/0/visits", 121, ["h1"]),
    ],
)
def test_nonexistent_or_mismatched_evidence_is_non_repairable(evidence: object) -> None:
    value = {
        "schema_version": 1,
        "outcome": "attributed",
        "conclusion": "A conclusion.",
        "hypotheses": [
            {"hypothesis_id": "h1", "statement": "A hypothesis.", "uncertainty": "Some."}
        ],
        "evidence": [evidence],
        "confidence": 0.8,
        "confidence_explanation": "Not a quality gate.",
        "abstained": False,
        "requested_data": [],
    }

    with pytest.raises(ProposalEvidenceError):
        validate_attribution_conclusion(value, tool_results=_tool_results())
