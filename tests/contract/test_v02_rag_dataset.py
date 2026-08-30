"""V0.2-T05 RAG dataset integrity and human-review contracts."""

from pathlib import Path

import pytest

from oria.eval import HumanReviewRequired, load_rag_dataset

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "eval" / "datasets" / "rag" / "v1.manifest.json"


def test_rag_v1_has_six_balanced_categories_and_frozen_splits() -> None:
    dataset = load_rag_dataset(_MANIFEST)

    assert dataset.manifest.case_count == 60
    assert dataset.manifest.development_case_count == 42
    assert dataset.manifest.holdout_case_count == 18
    assert dataset.manifest.contains_real_entities is False
    assert dataset.manifest.review_status == "approved"
    assert dataset.manifest.human_review_complete is True
    assert dataset.manifest.holdout_frozen is True
    assert dataset.manifest.baseline_created is True
    assert all(case.review.status == "approved" for case in dataset.cases)
    assert dataset.manifest.development_critical_case_count == 6
    assert dataset.manifest.holdout_critical_case_count == 6
    assert sum(case.critical for case in dataset.cases if case.split == "development") == 6
    assert sum(case.critical for case in dataset.cases if case.split == "holdout") == 6
    category_counts: dict[str, int] = {}
    for case in dataset.cases:
        category_counts[case.expected_rule_category] = (
            category_counts.get(case.expected_rule_category, 0) + 1
        )
    assert set(category_counts.values()) == {10}


def test_pending_rag_dataset_cannot_cross_the_human_review_gate(
    pending_rag_manifest: Path,
) -> None:
    with pytest.raises(HumanReviewRequired, match="pending actual human review"):
        load_rag_dataset(pending_rag_manifest)
