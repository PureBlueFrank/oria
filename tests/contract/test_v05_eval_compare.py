"""Contracts for fair single/multi comparison."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from oria.agent import initial_supervisor_state
from oria.core.types import Message
from oria.eval.compare import (
    ArchitectureBudgets,
    ComparisonBudget,
    ComparisonError,
    ComparisonJudgePacket,
    assert_preregistered_rubric,
    preregister_comparison_rubric,
    randomized_execution_order,
)
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset

pytestmark = pytest.mark.contract


def _budget(*, max_cost_usd: float = 5.0) -> ComparisonBudget:
    return ComparisonBudget(
        max_cases=2,
        max_input_tokens=200,
        max_output_tokens=100,
        max_cost_usd=max_cost_usd,
        max_wall_seconds=30,
        max_model_turns=12,
        max_tool_calls=12,
        max_total_tokens=300,
        per_case_max_model_turns=6,
        per_case_max_tool_calls=6,
        per_case_max_input_tokens=100,
        per_case_max_output_tokens=50,
        per_case_max_cost_usd=1.0,
        per_case_timeout_seconds=10,
    )


def test_comparison_rejects_any_unequal_architecture_budget() -> None:
    with pytest.raises(ValidationError, match="identical total budgets"):
        ArchitectureBudgets(single=_budget(), multi=_budget(max_cost_usd=6.0))


def test_random_order_is_seeded_replayable_and_complete() -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = load_golden_dataset(root / "eval/datasets/scenario_b/v2.manifest.json")
    cases = tuple(case for case in dataset.cases[:2] if isinstance(case, AttributionGoldenCase))

    first = randomized_execution_order(cases, repetitions=2, seed="fixed-seed")
    second = randomized_execution_order(cases, repetitions=2, seed="fixed-seed")

    assert first == second
    assert len(first) == 8
    assert {(slot.architecture, slot.case_id, slot.repetition) for slot in first} == {
        (architecture, case.case_id, repetition)
        for architecture in ("single", "multi")
        for case in cases
        for repetition in (1, 2)
    }


def test_judge_packet_cannot_contain_architecture_label() -> None:
    schema = ComparisonJudgePacket.model_json_schema()

    assert "architecture" not in schema["properties"]
    assert "architecture_label" not in schema["properties"]


def test_supervisor_history_is_defaulted_and_checkpoint_compatible() -> None:
    defaulted = initial_supervisor_state(user_request="归因分析", effective_at="2026-09-10")
    with_history = initial_supervisor_state(
        user_request="归因分析",
        effective_at="2026-09-10",
        conversation_history=(Message(role="user", content="prior question"),),
    )

    assert defaulted["conversation_history"] == []
    assert with_history["conversation_history"][0]["content"] == "prior question"
    del defaulted["conversation_history"]
    assert defaulted.get("conversation_history", []) == []


def test_preregistered_rubric_rejects_post_registration_change(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    rubric_path = tmp_path / "rubric.yaml"
    shutil.copyfile(root / "eval/config/attribution-rubric-v2.yaml", rubric_path)
    registered = preregister_comparison_rubric(rubric_path)
    rubric_path.write_text(
        rubric_path.read_text(encoding="utf-8") + "\n# post-run mutation\n",
        encoding="utf-8",
    )

    with pytest.raises(ComparisonError, match="changed after preregistration"):
        assert_preregistered_rubric(rubric_path, registered)
