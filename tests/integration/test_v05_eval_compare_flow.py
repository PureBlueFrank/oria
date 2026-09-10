"""Offline end-to-end coverage for the fair comparison harness."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from oria.cli import app
from oria.eval.compare import (
    ComparisonBudget,
    ComparisonFixturePlan,
    ComparisonReport,
)
from oria.eval.nightly import TokenPrices

pytestmark = pytest.mark.integration


def test_fixture_comparison_retains_all_runs_metrics_and_variance(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    budget = ComparisonBudget(
        max_cases=50,
        max_input_tokens=50_000,
        max_output_tokens=25_000,
        max_cost_usd=5.0,
        max_wall_seconds=300,
        max_model_turns=400,
        max_tool_calls=400,
        max_total_tokens=75_000,
        per_case_max_model_turns=8,
        per_case_max_tool_calls=8,
        per_case_max_input_tokens=1_000,
        per_case_max_output_tokens=500,
        per_case_max_cost_usd=0.1,
        per_case_timeout_seconds=20,
    )
    prices = TokenPrices(
        input_cache_hit_per_million_usd=0.1,
        input_cache_miss_per_million_usd=0.2,
        output_per_million_usd=0.4,
        reasoning_per_million_usd=0.4,
    )
    plan = ComparisonFixturePlan(
        pricing_snapshot_id="fixture-integration-v1",
        budget=budget,
        token_prices=prices,
    )
    budget_path = tmp_path / "budget.json"
    report_path = tmp_path / "report.json"
    budget_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "compare",
            "--manifest",
            str(root / "eval/datasets/scenario_b/v2.manifest.json"),
            "--rubric",
            str(root / "eval/config/attribution-rubric-v2.yaml"),
            "--repetitions",
            "1",
            "--seed",
            "integration-seed",
            "--budget",
            str(budget_path),
            "--data-dir",
            str(tmp_path / "run"),
            "--report",
            str(report_path),
        ],
    )
    assert result.exit_code == 0, result.output
    report = ComparisonReport.model_validate_json(report_path.read_text(encoding="utf-8"))

    assert len(report.runs) == 100
    assert report.single.run_count == report.multi.run_count == 50
    assert {run.architecture for run in report.runs} == {"single", "multi"}
    assert all(run.quality_score >= 0 for run in report.runs)
    assert all(run.cost_usd >= 0 and run.latency_ms >= 0 for run in report.runs)
    assert report.single.quality_variance == 0
    assert report.multi.latency_variance == 0
    assert report.significance == "descriptive_only"
    assert "best" not in report.model_dump(mode="json")
    if report.delta_multi_minus_single.quality <= 0:
        assert report.conclusion != "multi_improved"
        assert "gain" not in report.applicability_boundary.casefold()
