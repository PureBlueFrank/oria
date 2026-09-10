"""Live contracts for the fair single/multi comparison harness."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from oria.cli import app
from oria.eval import compare
from oria.eval.compare import (
    ArchitectureBudgets,
    ArchitectureRunResult,
    ComparisonError,
    ComparisonJudgePacket,
    ComparisonLiveBudget,
    ComparisonLiveConfig,
    ComparisonLiveTarget,
    run_comparison_live,
)
from oria.eval.nightly import PricingSnapshot, TieredModelPrices, TokenPrices

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "eval/datasets/scenario_b/v2.manifest.json"
RUBRIC = ROOT / "eval/config/attribution-rubric-v2.yaml"


def _budget(**updates: Any) -> ComparisonLiveBudget:
    values: dict[str, Any] = {
        "max_cases": 20,
        "max_model_requests": 160,
        "max_input_tokens": 2_000,
        "max_output_tokens": 1_000,
        "max_cost_usd": 20.0,
        "max_wall_seconds": 300,
        "per_case_max_model_turns": 8,
        "per_case_max_tool_calls": 10,
        "per_case_max_input_tokens": 100,
        "per_case_max_output_tokens": 50,
        "per_case_max_cost_usd": 1.0,
        "per_case_timeout_seconds": 30,
    }
    values.update(updates)
    return ComparisonLiveBudget.model_validate(values)


def _target(**updates: Any) -> ComparisonLiveTarget:
    values: dict[str, Any] = {
        "target_id": "codex-contract",
        "provider": "codex",
        "model": "contract-model",
        "credential_env": "ORIA_CONTRACT_AUTH",
        "pricing_snapshot_id": "contract-pricing-v1",
        "rate_tier": "peak",
        "repetitions": 1,
        "order_seed": "contract-order",
        "budget": _budget(),
    }
    values.update(updates)
    return ComparisonLiveTarget.model_validate(values)


def _pricing() -> PricingSnapshot:
    prices = TokenPrices(
        input_cache_hit_per_million_usd=0.1,
        input_cache_miss_per_million_usd=0.2,
        output_per_million_usd=0.4,
        reasoning_per_million_usd=0.4,
    )
    return PricingSnapshot(
        snapshot_id="contract-pricing-v1",
        currency="USD",
        unit="per_million_tokens",
        source_url="https://oria.invalid/comparison-contract",
        verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2036, 1, 1, tzinfo=UTC),
        models={"contract-model": TieredModelPrices(peak=prices, off_peak=prices)},
    )


def _runtime(llm: object) -> Any:
    return SimpleNamespace(
        llm=llm,
        config=SimpleNamespace(llm=SimpleNamespace(provider="codex", model="contract-model")),
    )


def test_live_config_requires_unique_targets_and_configured_recommendation() -> None:
    target = _target()
    valid = ComparisonLiveConfig(recommended_target=target.target_id, targets=(target,))
    assert valid.targets == (target,)

    with pytest.raises(ValidationError, match="non-empty and unique"):
        ComparisonLiveConfig(targets=(target, target))
    with pytest.raises(ValidationError, match="must be configured"):
        ComparisonLiveConfig(recommended_target="missing", targets=(target,))
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        _target(target_id="INVALID TARGET")


def test_live_architectures_require_equal_budget() -> None:
    with pytest.raises(ValidationError, match="identical total budgets"):
        ArchitectureBudgets(single=_budget(), multi=_budget(max_wall_seconds=301))


@pytest.mark.asyncio
async def test_live_path_wires_same_llm_to_both_architectures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm = object()
    calls: list[tuple[str, object | None]] = []

    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        slot = args[0]
        calls.append((slot.architecture, kwargs["llm"]))
        return ArchitectureRunResult(
            architecture=slot.architecture,
            case_id=kwargs["case"].case_id,
            repetition=slot.repetition,
            blind_item_id=f"blind-{slot.position}",
            quality_score=0.5,
            model_turns=1,
            tool_calls=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost_usd=0,
            latency_ms=1,
        )

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    report = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(stub_llm),
        target=_target(),
        data_dir=tmp_path,
        pricing_snapshot=_pricing(),
    )

    assert report.run_status == "completed"
    assert {architecture for architecture, _ in calls} == {"single", "multi"}
    assert all(llm is stub_llm for _, llm in calls)
    assert len(calls) == 40
    assert report.single is not None and report.single.run_count == 20
    assert report.multi is not None and report.multi.run_count == 20


@pytest.mark.asyncio
async def test_live_budget_ledger_rejects_usage_above_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def oversized_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        slot = args[0]
        return ArchitectureRunResult(
            architecture=slot.architecture,
            case_id=kwargs["case"].case_id,
            repetition=slot.repetition,
            blind_item_id=f"blind-{slot.position}",
            quality_score=0,
            model_turns=1,
            tool_calls=0,
            input_tokens=101,
            output_tokens=1,
            total_tokens=102,
            cost_usd=0,
            latency_ms=1,
        )

    monkeypatch.setattr(compare, "run_architecture_slot", oversized_slot)
    with pytest.raises(ComparisonError, match="reserved hard budget"):
        await run_comparison_live(
            MANIFEST,
            rubric_path=RUBRIC,
            base_runtime=_runtime(object()),
            target=_target(),
            data_dir=tmp_path,
            pricing_snapshot=_pricing(),
            max_new_case_runs=1,
        )


def test_live_judge_packet_schema_hides_architecture() -> None:
    properties = ComparisonJudgePacket.model_json_schema()["properties"]
    assert "architecture" not in properties
    assert "architecture_label" not in properties


def test_compare_cli_rejects_unknown_live_target_and_community() -> None:
    runner = CliRunner()
    unknown = runner.invoke(
        app,
        ["eval", "compare", "--verification", "live", "--target", "unknown-target"],
    )
    community = runner.invoke(app, ["eval", "compare", "--verification", "community"])

    assert unknown.exit_code == 2
    assert "eval_compare_blocked" in unknown.output
    assert "not configured" in unknown.output
    assert community.exit_code == 2
    assert "eval_compare_blocked" in community.output
    assert "Community comparison is disabled" in community.output
