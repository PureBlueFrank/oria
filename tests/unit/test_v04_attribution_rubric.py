"""Unit contracts for the Scenario B blind attribution rubric."""

from __future__ import annotations

from pathlib import Path

from oria.eval import load_attribution_rubric


def test_attribution_rubric_is_weighted_and_hides_labels_and_architecture() -> None:
    root = Path(__file__).resolve().parents[2]
    rubric = load_attribution_rubric(root / "eval/config/attribution-rubric-v1.yaml")

    assert rubric.suite == "attribution"
    assert sum(criterion.weight for criterion in rubric.criteria) == 1.0
    assert rubric.calibration.required is True
    assert rubric.calibration.minimum_human_sample >= 10
    assert {
        "root_cause_code",
        "golden_rationale",
        "provider_profile",
        "architecture_label",
    } <= set(rubric.hidden_fields)
    assert not set(rubric.hidden_fields).intersection(rubric.blind_input_fields)
