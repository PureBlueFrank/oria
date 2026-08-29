"""Versioned deterministic evaluation dataset contracts."""

from oria.eval.datasets import (
    GoldenCase,
    GoldenDataset,
    GoldenDatasetError,
    HumanReviewRequired,
    load_golden_dataset,
)
from oria.eval.scenario_a import (
    GoldenGateError,
    ScenarioABaseline,
    ScenarioACaseResult,
    ScenarioAGates,
    ScenarioAMetrics,
    ScenarioAReport,
    assert_scenario_a_gates,
    create_scenario_a_baseline,
    load_scenario_a_baseline,
    load_scenario_a_gates,
    run_scenario_a_golden,
    write_value_model,
)

__all__ = [
    "GoldenCase",
    "GoldenDataset",
    "GoldenDatasetError",
    "GoldenGateError",
    "HumanReviewRequired",
    "ScenarioABaseline",
    "ScenarioACaseResult",
    "ScenarioAGates",
    "ScenarioAMetrics",
    "ScenarioAReport",
    "assert_scenario_a_gates",
    "create_scenario_a_baseline",
    "load_golden_dataset",
    "load_scenario_a_baseline",
    "load_scenario_a_gates",
    "run_scenario_a_golden",
    "write_value_model",
]
