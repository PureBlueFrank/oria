from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from oria.eval import (
    AttributionLiveCaseRecord,
    AttributionLiveConfig,
    attribution_calibration,
    attribution_coverage_risk,
    attribution_live_metrics,
    attribution_live_variance,
    load_attribution_live_config,
    preflight_attribution_live,
)

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "eval/config/attribution-live-v1.yaml"
PRICING = ROOT / "eval/config/pricing"
NOW = datetime.fromisoformat("2026-09-05T19:00:00+08:00")


def _record(
    *,
    case_id: str,
    repetition: int,
    expected: str,
    observed: str,
    expected_abstain: bool,
    observed_abstain: bool,
    confidence: float,
) -> AttributionLiveCaseRecord:
    passed = expected == observed and expected_abstain == observed_abstain
    return AttributionLiveCaseRecord.model_validate(
        {
            "case_id": case_id,
            "repetition": repetition,
            "expected_outcome": expected,
            "observed_outcome": observed,
            "expected_abstain": expected_abstain,
            "observed_abstain": observed_abstain,
            "critical": False,
            "automated_pass": passed,
            "failures": [] if passed else ["outcome_mismatch"],
            "required_tool_coverage": 1.0,
            "forbidden_tool_safe": True,
            "evidence_grounded": True,
            "confidence": confidence,
            "decision_confidence": 1 - confidence if observed_abstain else confidence,
            "conclusion": None,
            "tool_results": {},
            "events": [],
            "provider_request_ids": [f"req-{case_id}-{repetition}"],
            "provider_models": ["deepseek-v4-flash"],
            "model_turns": 1,
            "tool_calls_total": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_usd": 0.001,
            "cost_basis": "pricing_upper_bound",
            "latency_ms": 10 * repetition,
        }
    )


def _records() -> tuple[AttributionLiveCaseRecord, ...]:
    records: list[AttributionLiveCaseRecord] = []
    for repetition in (1, 2, 3):
        records.extend(
            (
                _record(
                    case_id="sb-v1-044",
                    repetition=repetition,
                    expected="attributed",
                    observed="attributed",
                    expected_abstain=False,
                    observed_abstain=False,
                    confidence=0.8,
                ),
                _record(
                    case_id="sb-v1-043",
                    repetition=repetition,
                    expected="conflicting",
                    observed="conflicting",
                    expected_abstain=False,
                    observed_abstain=False,
                    confidence=0.6,
                ),
                _record(
                    case_id="sb-v1-031",
                    repetition=repetition,
                    expected="insufficient",
                    observed="attributed" if repetition == 3 else "insufficient",
                    expected_abstain=True,
                    observed_abstain=repetition != 3,
                    confidence=0.2,
                ),
            )
        )
    return tuple(records)


def test_live_config_freezes_full_holdout_three_times_with_hard_budget() -> None:
    config = load_attribution_live_config(CONFIG)
    target = config.targets[0]

    assert target.split == "holdout"
    assert target.repetitions == 3
    assert target.budget.max_cases == 60
    assert target.budget.max_model_requests == 240
    assert target.budget.max_input_tokens == 5_760_000
    assert target.budget.max_output_tokens == 1_440_000
    assert target.budget.max_cost_usd == 1.35
    assert target.budget.per_case_max_model_turns == 4
    assert target.budget.per_case_max_tool_calls == 10
    assert target.dataset_sha256 == (
        "b59078792fd00c3fff916183679402cf4cc340da98f7da27bee3dcea4923e40d"
    )
    assert target.baseline_fingerprint == (
        "sha256:9daf97daf230cc40fc064bac7eaecb2d18f82b3cb790121175bf7ba2e99d87c4"
    )


def test_live_budget_rejects_aggregate_bound_below_case_reservations() -> None:
    config = load_attribution_live_config(CONFIG)
    payload = config.model_dump(mode="json")
    payload["targets"][0]["budget"]["max_input_tokens"] = 1

    with pytest.raises(ValidationError, match="worst-case reservations"):
        AttributionLiveConfig.model_validate(payload)


def test_live_preflight_is_request_free_and_fails_closed_without_credential() -> None:
    card = preflight_attribution_live(
        config_path=CONFIG,
        pricing_dir=PRICING,
        target_id="deepseek",
        environ={},
        now=NOW,
        known_targets=frozenset({"deepseek"}),
    )

    assert card.status == "blocked"
    assert card.request_count == 0
    assert card.reason == "attribution Live credential is missing"


def test_live_preflight_accepts_only_reviewed_frozen_identity_before_provider_setup() -> None:
    card = preflight_attribution_live(
        config_path=CONFIG,
        pricing_dir=PRICING,
        target_id="deepseek",
        environ={"DEEPSEEK_API_KEY": "test-presence-only"},
        now=NOW,
        known_targets=frozenset({"deepseek"}),
    )

    assert card.status == "ready"
    assert card.request_count == 0
    assert card.holdout_case_count == 20
    assert card.repetitions == 3
    assert card.expected_case_runs == 60


def test_live_metrics_variance_calibration_and_coverage_risk_are_descriptive() -> None:
    records = _records()

    metrics = attribution_live_metrics(records)
    variance = attribution_live_variance(records)
    calibration = attribution_calibration(records)
    curve = attribution_coverage_risk(records)

    assert metrics.case_runs == 9
    assert metrics.unique_cases == 3
    assert metrics.automated_pass_rate == pytest.approx(8 / 9)
    assert metrics.answer_coverage == pytest.approx(7 / 9)
    assert variance.per_case_outcome_consistency_rate == pytest.approx(2 / 3)
    assert variance.automated_pass_rate_stddev > 0
    assert calibration.status == "descriptive_only"
    assert calibration.gate_eligible is False
    assert calibration.sample_size == 9
    assert calibration.brier_score is not None
    assert calibration.expected_calibration_error is not None
    assert [point.threshold for point in curve] == [0.0, 0.5, 0.7, 0.8, 0.9]
    assert curve[0].coverage == 1.0
