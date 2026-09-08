"""V2 Scenario B golden dataset integrity and capability-balance tests."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from oria.eval.datasets import (
    AttributionGoldenCase,
    load_golden_dataset,
    required_tools_for,
)

pytestmark = pytest.mark.contract

_MANIFEST = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "scenario_b" / "v2.manifest.json"
)

_OUTCOME_FOR_ANSWERABILITY = {
    "supported": "attributed",
    "conflicting": "conflicting",
    "insufficient": "insufficient",
}


def _dataset():
    return load_golden_dataset(_MANIFEST, require_human_review=False)


def test_v2_manifest_loads_with_version_two() -> None:
    dataset = _dataset()
    assert dataset.manifest.suite == "scenario_b"
    assert dataset.manifest.dataset_version == "2"
    assert dataset.manifest.case_count == 50


def test_v2_split_is_thirty_twenty() -> None:
    dataset = _dataset()
    development = [c for c in dataset.cases if c.split == "development"]
    holdout = [c for c in dataset.cases if c.split == "holdout"]
    assert len(development) == 30
    assert len(holdout) == 20


def test_v2_cases_carry_answerability_and_task_type() -> None:
    dataset = _dataset()
    for case in dataset.cases:
        assert case.answerability is not None
        assert case.task_type is not None
        assert case.expected_outcome == _OUTCOME_FOR_ANSWERABILITY[case.answerability]


def test_v2_conflicting_balanced_across_splits() -> None:
    dataset = _dataset()
    development = [c for c in dataset.cases if c.split == "development"]
    holdout = [c for c in dataset.cases if c.split == "holdout"]
    dev_conflicting = [c for c in development if c.expected_outcome == "conflicting"]
    holdout_conflicting = [c for c in holdout if c.expected_outcome == "conflicting"]
    assert len(dev_conflicting) >= 3
    assert len(holdout_conflicting) >= 4


def test_v2_security_capabilities_span_both_splits() -> None:
    dataset = _dataset()
    development = [c for c in dataset.cases if c.split == "development"]
    holdout = [c for c in dataset.cases if c.split == "holdout"]
    for task_type in ("tenant_access_control", "tool_policy", "prompt_injection"):
        dev = [c for c in development if c.task_type == task_type]
        hold = [c for c in holdout if c.task_type == task_type]
        assert len(dev) >= 1, f"{task_type} missing in development"
        assert len(hold) >= 1, f"{task_type} missing in holdout"


def test_v2_required_and_optional_tools_are_disjoint() -> None:
    dataset = _dataset()
    for case in dataset.cases:
        assert not set(case.required_tools).intersection(case.optional_tools)
        assert not set(case.forbidden_tools).intersection(case.required_tools)


def test_v2_required_tools_resolve_for_every_case() -> None:
    dataset = _dataset()
    for case in dataset.cases:
        resolved = required_tools_for(case)
        if case.expected_outcome in {"attributed", "conflicting"}:
            assert resolved, f"{case.case_id} answered case has no required tools"
        assert set(resolved).issubset(
            {
                "query_funnel",
                "drill_down",
                "query_activity",
                "query_market_overview",
                "search_history_experience",
            }
        )


def test_v2_035_allows_proactive_verification_without_requiring_it() -> None:
    dataset = _dataset()
    case = next(c for c in dataset.cases if c.case_id == "sb-v2-035")
    assert case.task_type == "evidence_integrity"
    assert case.expected_outcome == "insufficient"
    assert case.required_tools == ()
    assert set(case.optional_tools) == {"query_funnel", "query_activity"}


def test_v2_011_asserts_merchant_granularity_gap() -> None:
    dataset = _dataset()
    case = next(c for c in dataset.cases if c.case_id == "sb-v2-011")
    assert case.task_type == "evidence_abstention"
    assert any("merchant_id" in item for item in case.required_evidence)
    assert any("无匹配" in item for item in case.required_evidence)


def test_v2_043_does_not_ask_a_leading_binary_question() -> None:
    dataset = _dataset()
    case = next(c for c in dataset.cases if c.case_id == "sb-v2-043")
    assert case.expected_outcome == "conflicting"
    assert "能否唯一归因" in case.question
    assert "能区分因果吗" not in case.question
    assert len(case.acceptable_hypotheses) >= 2


def test_v2_north_entities_exist_and_are_holdout() -> None:
    dataset = _dataset()
    north_ids = {"sb-v2-051", "sb-v2-052"}
    north = [c for c in dataset.cases if c.case_id in north_ids]
    assert len(north) == 2
    assert all(c.split == "holdout" for c in north)
    assert {c.fixture_variant for c in north} == {
        "north_campaign_effect",
        "north_market_conflict",
    }


def test_v2_rubric_is_version_two() -> None:
    from oria.eval.attribution import load_attribution_rubric

    rubric_path = _MANIFEST.parents[2] / "config" / "attribution-rubric-v2.yaml"
    rubric = load_attribution_rubric(rubric_path)
    assert rubric.rubric_version == "2"
    assert rubric.dataset_version == "2"


def test_v2_outcome_distribution_rebalances_conflicting() -> None:
    dataset = _dataset()
    counts = Counter(c.expected_outcome for c in dataset.cases)
    assert counts["conflicting"] >= 8
    assert counts["attributed"] >= 18
    assert counts["insufficient"] >= 10
    assert isinstance(dataset.cases[0], AttributionGoldenCase)


def test_v2_approved_dataset_crosses_human_review_gate() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=True)
    assert dataset.manifest.review_status == "approved"
    assert dataset.manifest.human_review_complete is True


def test_v2_manifest_records_frozen_holdout_and_baseline() -> None:
    dataset = _dataset()
    assert dataset.manifest.holdout_frozen is True
    assert dataset.manifest.baseline_created is True
    assert {c.review.reviewed_by for c in dataset.cases} == {"FrankLee"}


def test_v2_no_fixture_family_leaks_across_splits() -> None:
    """A non-standard fixture variant must appear in only one split, so holdout
    cases never share the injected anomaly numbers with development cases."""
    from collections import defaultdict

    dataset = _dataset()
    by_variant: dict[str, set[str]] = defaultdict(set)
    for case in dataset.cases:
        by_variant[case.fixture_variant].add(case.split)
    leaks = [v for v, splits in by_variant.items() if v != "standard" and len(splits) > 1]
    assert leaks == [], f"fixture families leaked across splits: {leaks}"
