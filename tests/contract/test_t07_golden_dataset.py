"""Scenario A Golden dataset integrity and human-review gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.eval import HumanReviewRequired, load_golden_dataset

pytestmark = pytest.mark.contract

_MANIFEST = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "scenario_a" / "v1.manifest.json"
)


def test_golden_draft_has_thirty_unique_critical_synthetic_cases() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert len(dataset.cases) == 30
    assert len({case.case_id for case in dataset.cases}) == 30
    assert all(case.critical for case in dataset.cases)
    assert dataset.manifest.source == "synthetic"
    assert dataset.manifest.contains_real_entities is False
    assert dataset.manifest.baseline_created is False


def test_unreviewed_golden_cannot_be_used_to_create_a_baseline() -> None:
    with pytest.raises(HumanReviewRequired, match="actual human review"):
        load_golden_dataset(_MANIFEST)
