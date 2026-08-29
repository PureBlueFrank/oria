"""Scenario A Golden dataset integrity and human-review gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.eval import load_golden_dataset

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
    assert dataset.manifest.review_status == "approved"
    assert dataset.manifest.human_review_complete is True
    assert dataset.manifest.baseline_created is True
    assert all(case.review.status == "approved" for case in dataset.cases)
    assert {case.review.reviewed_by for case in dataset.cases} == {"FrankLee"}
    assert all(case.review.reviewed_at is not None for case in dataset.cases)


def test_approved_golden_can_cross_the_human_review_gate() -> None:
    dataset = load_golden_dataset(_MANIFEST)

    assert dataset.manifest.review_status == "approved"


def test_write_tool_prompt_injection_is_a_fail_closed_golden_case() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    case = next(item for item in dataset.cases if item.case_id == "sa-v1-027")

    assert case.fixture_variant == "prompt_injection:write_tool"
    assert case.expected_outcome == "runtime_failure"
    assert case.expected_reason == "policy_or_contract_violation"
    assert case.expected_tools == ()
    assert case.forbidden_tools == ("persist_campaign",)
