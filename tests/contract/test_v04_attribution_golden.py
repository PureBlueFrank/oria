"""V0.4-T04 Scenario B golden dataset integrity, review gate, and isolation tests."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from oria.eval.datasets import (
    AttributionGoldenCase,
    load_golden_dataset,
)

pytestmark = pytest.mark.contract

_MANIFEST = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "scenario_b" / "manifest.json"
)
_DATASET = _MANIFEST.parent / "v1.jsonl"


# ─── Phase 1: Golden mechanism generalization ───


def test_scenario_b_manifest_loads_with_suite_b() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert dataset.manifest.suite == "scenario_b"
    assert dataset.manifest.source == "synthetic"
    assert dataset.manifest.contains_real_entities is False
    assert dataset.manifest.license == "CC0-1.0"
    assert dataset.manifest.schema_version == 1


def test_scenario_b_cases_use_attribution_golden_case_schema() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert all(isinstance(c, AttributionGoldenCase) for c in dataset.cases)


def test_scenario_b_case_count_matches_manifest() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert len(dataset.cases) == dataset.manifest.case_count
    assert len(dataset.cases) >= 50


def test_scenario_b_case_ids_are_unique_and_follow_pattern() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    ids = [c.case_id for c in dataset.cases]
    assert len(ids) == len(set(ids))
    assert all(c.case_id.startswith("sb-v1-") for c in dataset.cases)


def test_scenario_b_critical_count_matches_manifest() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    critical_count = sum(1 for c in dataset.cases if c.critical)
    assert critical_count == dataset.manifest.critical_case_count
    assert critical_count >= 1


def test_scenario_b_sha256_integrity_check_passes() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    payload = _DATASET.read_bytes()
    import hashlib

    assert hashlib.sha256(payload).hexdigest() == dataset.manifest.dataset_sha256


# ─── Phase 1: Human review gate ───


def test_approved_dataset_crosses_the_human_review_gate() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=True)

    assert dataset.manifest.review_status == "approved"


def test_all_cases_record_the_actual_human_review() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert all(c.review.status == "approved" for c in dataset.cases)
    assert {c.review.reviewed_by for c in dataset.cases} == {"FrankLee"}
    assert {c.review.reviewed_at.isoformat() for c in dataset.cases if c.review.reviewed_at} == {
        "2026-09-05T18:46:54+08:00"
    }


def test_manifest_records_frozen_holdout_and_created_baseline() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    assert dataset.manifest.review_status == "approved"
    assert dataset.manifest.human_review_complete is True
    assert dataset.manifest.baseline_created is True
    assert dataset.manifest.holdout_frozen is True


def test_scenario_b_has_preassigned_development_and_holdout_splits() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    development = [case for case in dataset.cases if case.split == "development"]
    holdout = [case for case in dataset.cases if case.split == "holdout"]
    assert len(development) == dataset.manifest.development_case_count == 30
    assert len(holdout) == dataset.manifest.holdout_case_count == 20
    assert {case.case_id for case in development}.isdisjoint(case.case_id for case in holdout)


def test_scenario_b_manifest_binds_the_blind_rubric_by_hash() -> None:
    import hashlib

    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    rubric = _MANIFEST.parents[2] / "config" / str(dataset.manifest.rubric_file)

    assert rubric.name == "attribution-rubric-v1.yaml"
    assert hashlib.sha256(rubric.read_bytes()).hexdigest() == dataset.manifest.rubric_sha256


# ─── Phase 1: Case schema validation ───


def test_insufficient_cases_require_abstain_and_no_root_cause() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    insufficient = [c for c in dataset.cases if c.expected_outcome == "insufficient"]
    assert len(insufficient) > 0
    for case in insufficient:
        assert case.expected_abstain is True
        assert case.root_cause_code is None


def test_attributed_cases_require_hypotheses_evidence_and_tools() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    attributed = [c for c in dataset.cases if c.expected_outcome == "attributed"]
    assert len(attributed) > 0
    for case in attributed:
        assert case.expected_abstain is False
        assert len(case.acceptable_hypotheses) >= 1
        assert len(case.required_evidence) >= 1
        assert len(case.expected_tools) >= 1


def test_conflicting_cases_require_multiple_hypotheses() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    # Exercise the schema, not a fabricated conflict in the standard facts.
    payload = dataset.cases[1].model_dump(mode="json")
    payload["expected_outcome"] = "conflicting"
    with pytest.raises(ValueError, match="multiple acceptable hypotheses"):
        AttributionGoldenCase.model_validate(payload)
    payload["acceptable_hypotheses"].append("测试专用候选, 不代表现有事实存在冲突")
    case = AttributionGoldenCase.model_validate(payload)
    assert not case.expected_abstain
    assert len(case.acceptable_hypotheses) == 2


# ─── Phase 2: Six-category coverage ───


def test_evidence_review_records_actual_outcomes_without_fabricated_coverage() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    outcome_counts = Counter(c.expected_outcome for c in dataset.cases)
    assert outcome_counts == {"attributed": 20, "insufficient": 24, "conflicting": 6}
    assert sum(case.root_cause_code is not None for case in dataset.cases) == 8
    assert dataset.manifest.holdout_frozen


def test_privileged_dimension_cases_exist_and_expect_abstain() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    # Cases sb-v1-027 through sb-v1-032 cover privileged dimension
    privileged_ids = {f"sb-v1-{i:03d}" for i in range(27, 33)}
    privileged = [c for c in dataset.cases if c.case_id in privileged_ids]
    assert len(privileged) == 6
    for case in privileged:
        assert case.expected_outcome == "insufficient"
        assert case.expected_abstain is True


def test_injection_cases_exist_and_expect_abstain() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    # Cases sb-v1-033 through sb-v1-038 cover prompt injection
    injection_ids = {f"sb-v1-{i:03d}" for i in range(33, 39)}
    injection_cases = [c for c in dataset.cases if c.case_id in injection_ids]
    assert len(injection_cases) == 6
    for case in injection_cases:
        assert case.expected_outcome == "insufficient"
        assert case.expected_abstain is True


def test_followup_cases_include_actual_multi_turn_context() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    followup_cases = [c for c in dataset.cases if "接上一轮" in c.question]
    assert len(followup_cases) == 6
    assert all(
        tuple(message.role for message in case.conversation_history) == ("user", "assistant")
        for case in followup_cases
    )


# ─── Phase 2: Data isolation / contamination checks ───


def test_root_cause_codes_do_not_appear_in_production_prompt() -> None:
    """Labels/rationale must not leak into the attribution prompt or production agent."""
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)

    root = Path(__file__).resolve().parents[2]
    production_files = (
        root / "src" / "oria" / "agent" / "attribution.py",
        root / "src" / "oria" / "agent" / "graph.py",
        root / "src" / "oria" / "agent" / "models.py",
        root / "src" / "oria" / "tools" / "analytics.py",
        root / "src" / "oria" / "prompts" / "attribution_reasoning" / "v1.jinja",
    )
    production_text = "\n".join(p.read_text(encoding="utf-8") for p in production_files)

    for case in dataset.cases:
        if case.root_cause_code is not None:
            assert case.root_cause_code not in production_text, (
                f"root_cause_code '{case.root_cause_code}' leaked into production code"
            )
        assert case.golden_rationale not in production_text, (
            f"golden_rationale leaked into production code for case {case.case_id}"
        )


def test_eval_labels_not_imported_by_production_analytics() -> None:
    """Production analytics package must not import eval label modules."""
    root = Path(__file__).resolve().parents[2]
    analytics_dir = root / "src" / "oria" / "analytics"
    imports: set[str] = set()
    for source_path in analytics_dir.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
    assert all(not name.startswith("oria.eval") for name in imports)


def test_golden_rationale_not_in_query_database_fixture(tmp_path: Path) -> None:
    """Rationale must only exist in the eval label library, not the query database."""
    from oria.eval.attribution_data import generate_attribution_fixture

    query_db = tmp_path / "analytics.db"
    label_db = tmp_path / "labels.db"
    generate_attribution_fixture(query_db, label_db)

    query_bytes = query_db.read_bytes()
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    for case in dataset.cases:
        assert case.golden_rationale.encode() not in query_bytes, (
            f"rationale for {case.case_id} found in query database"
        )


# ─── Regression: Scenario A golden must still work ───


_SCENARIO_A_MANIFEST = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "scenario_a" / "v1.manifest.json"
)


def test_scenario_a_golden_still_loads_and_is_approved() -> None:
    """Generalization must not break scenario A."""
    dataset = load_golden_dataset(_SCENARIO_A_MANIFEST)

    assert dataset.manifest.suite == "scenario_a"
    assert dataset.manifest.review_status == "approved"
    assert all(c.review.status == "approved" for c in dataset.cases)
    # Ensure scenario A cases are GoldenCase, not AttributionGoldenCase
    from oria.eval.datasets import GoldenCase

    assert all(isinstance(c, GoldenCase) for c in dataset.cases)


def test_scenario_a_and_b_use_different_case_schemas() -> None:
    """Suite dispatch must select the correct case model."""
    ds_a = load_golden_dataset(_SCENARIO_A_MANIFEST, require_human_review=False)
    ds_b = load_golden_dataset(_MANIFEST, require_human_review=False)

    from oria.eval.datasets import AttributionGoldenCase, GoldenCase

    assert isinstance(ds_a.cases[0], GoldenCase)
    assert isinstance(ds_b.cases[0], AttributionGoldenCase)
    assert not isinstance(ds_a.cases[0], AttributionGoldenCase)
    assert not isinstance(ds_b.cases[0], GoldenCase)
