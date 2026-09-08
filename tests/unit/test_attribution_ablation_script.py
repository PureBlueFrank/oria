from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/run_attribution_ablation.py"


def _module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("run_attribution_ablation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ablation_has_preregistered_two_by_two_groups() -> None:
    module = _module()
    assert [group.group_id for group in module._GROUPS] == [
        "g00_control",
        "g01_budget",
        "g10_model",
        "g11_both",
    ]
    assert {
        (group.model, group.reasoning_effort, group.max_model_turns, group.max_tool_calls)
        for group in module._GROUPS
    } == {
        ("deepseek-v4-flash", "none", 4, 10),
        ("deepseek-v4-flash", "none", 6, 20),
        ("deepseek-v4-pro", "high", 4, 10),
        ("deepseek-v4-pro", "high", 6, 20),
    }


def test_ablation_only_allows_the_exposed_holdout_case() -> None:
    module = _module()
    case = module._load_case(ROOT / "eval/datasets/scenario_b/manifest.json", "sb-v1-043")
    assert case.fixture_variant == "mixed_funnel"
    with pytest.raises(ValueError, match="only permits"):
        module._load_case(ROOT / "eval/datasets/scenario_b/manifest.json", "sb-v1-044")


def test_semantic_pass_requires_both_independent_stages() -> None:
    module = _module()
    base = {
        "automated_pass": True,
        "conclusion": {
            "outcome": "conflicting",
            "conclusion": None,
            "hypotheses": [{"hypothesis_id": "h1"}, {"hypothesis_id": "h2"}],
            "causal_assessment": {
                "anomalous_conversion_stages": [
                    "impression_to_visit",
                    "confirmation_to_redemption",
                ],
                "shared_mechanism_observed": False,
            },
        },
    }
    assert module._semantic_status(base) == (True, None)
    base["conclusion"]["causal_assessment"]["anomalous_conversion_stages"] = ["impression_to_visit"]
    assert module._semantic_status(base) == (False, "semantic_missed_downstream")
