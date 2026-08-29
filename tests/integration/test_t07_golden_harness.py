"""Deterministic end-to-end regression coverage for the approved Scenario A suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.eval import (
    GoldenGateError,
    assert_scenario_a_gates,
    load_scenario_a_baseline,
    load_scenario_a_gates,
    run_scenario_a_golden,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "eval" / "datasets" / "scenario_a" / "v1.manifest.json"
_BASELINE = _ROOT / "eval" / "baselines" / "scenario_a" / "1.json"
_GATES = _ROOT / "eval" / "config" / "gates.yaml"


@pytest.mark.asyncio
async def test_all_approved_cases_match_the_frozen_baseline(tmp_path: Path) -> None:
    report = await run_scenario_a_golden(_MANIFEST, data_dir=tmp_path / "data")
    baseline = load_scenario_a_baseline(_BASELINE)
    gates = load_scenario_a_gates(_GATES)

    assert_scenario_a_gates(report, gates=gates, baseline=baseline)
    assert len(report.cases) == 30
    assert all(case.passed for case in report.cases)
    assert report.metrics.case_pass_rate == 1.0
    assert report.metrics.critical_pass_rate == 1.0

    regressed = report.model_copy(
        update={
            "metrics": report.metrics.model_copy(update={"case_pass_rate": 29 / 30}),
        }
    )
    with pytest.raises(GoldenGateError, match="required metric failed"):
        assert_scenario_a_gates(regressed, gates=gates, baseline=baseline)
