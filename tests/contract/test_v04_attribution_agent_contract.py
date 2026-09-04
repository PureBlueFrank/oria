"""V0.4-T03 public attribution-agent contracts."""

from __future__ import annotations

import pytest

from oria.agent import (
    ATTRIBUTION_TOOL_NAMES,
    attribution_conclusion_schema,
    attribution_research_spec,
    initial_attribution_state,
)

pytestmark = pytest.mark.contract


def test_attribution_spec_fixes_prompt_tools_output_and_strict_schema() -> None:
    spec = attribution_research_spec()
    schema = attribution_conclusion_schema()

    assert spec.prompt_name == "attribution_reasoning"
    assert spec.prompt_version == 1
    assert spec.tool_names == ATTRIBUTION_TOOL_NAMES
    assert spec.response_schema == schema
    assert spec.output_field == "conclusion"
    assert schema.strict is True
    assert schema.json_schema["additionalProperties"] is False
    assert set(schema.json_schema["properties"]) == {
        "schema_version",
        "outcome",
        "conclusion",
        "hypotheses",
        "evidence",
        "confidence",
        "confidence_explanation",
        "abstained",
        "requested_data",
    }


def test_attribution_private_loop_state_is_checkpoint_serializable() -> None:
    state = initial_attribution_state(
        question="Why did conversion change?",
        analysis_period="2026-08-30/2026-08-31",
    )

    for field in (
        "model_turns",
        "tool_calls_total",
        "validation_repairs",
        "seen_evidence_fingerprints",
        "no_progress_streak",
        "termination",
        "tool_results",
        "final_result",
        "conclusion",
    ):
        assert field in state
