"""V0.4-T03 public attribution-agent contracts."""

from __future__ import annotations

import pytest

from oria.agent import (
    ATTRIBUTION_TOOL_NAMES,
    attribution_conclusion_schema,
    attribution_research_spec,
    initial_attribution_state,
)
from oria.core.types import Message

pytestmark = pytest.mark.contract


def test_attribution_spec_fixes_prompt_tools_output_and_strict_schema() -> None:
    spec = attribution_research_spec()
    schema = attribution_conclusion_schema()

    assert spec.prompt_name == "attribution_reasoning"
    assert spec.prompt_version == 3
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
        "causal_assessment",
        "decision_assessment",
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


def test_attribution_state_includes_complete_conversation_history_before_current_turn() -> None:
    history = (
        Message(role="user", content="先看华东正餐是否有异常。"),
        Message(role="assistant", content="已发现异常, 但活动结束仍是待验证候选。"),
    )
    state = initial_attribution_state(
        question="那 08-31 的核销率是多少?",
        analysis_period="2026-08-30/2026-08-31",
        conversation_history=history,
    )

    messages = state["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == history[0].content
    assert messages[2]["content"] == history[1].content
    assert messages[3]["content"] == "那 08-31 的核销率是多少?"


def test_attribution_state_rejects_incomplete_conversation_history() -> None:
    with pytest.raises(ValueError, match="complete user/assistant pairs"):
        initial_attribution_state(
            question="继续分析",
            analysis_period="2026-08-30/2026-08-31",
            conversation_history=(Message(role="user", content="上一轮问题"),),
        )


def test_attribution_state_injects_tenant_context_when_provided() -> None:
    state = initial_attribution_state(
        question="查询 tenant-secondary 的数据。",
        analysis_period="2026-08-30/2026-08-31",
        tenant_id="local-community",
    )

    messages = state["messages"]
    assert [message["role"] for message in messages] == ["system", "system", "user"]
    assert "local-community" in messages[1]["content"]
    assert "其他租户" in messages[1]["content"]


def test_attribution_state_omits_tenant_context_by_default() -> None:
    state = initial_attribution_state(
        question="分析华东正餐。",
        analysis_period="2026-08-30/2026-08-31",
    )

    assert [message["role"] for message in state["messages"]] == ["system", "user"]
