"""Live contracts for the fair single/multi comparison harness."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from oria.cli import app
from oria.eval import compare
from oria.eval.attribution import AttributionBlindItem
from oria.eval.compare import (
    ArchitectureBudgets,
    ArchitectureRunResult,
    ComparisonError,
    ComparisonJudgePacket,
    ComparisonLiveBudget,
    ComparisonLiveConfig,
    ComparisonLiveTarget,
    derive_comparison_blind_item_id,
    run_comparison_live,
    shuffled_blind_review_packets,
    validate_comparison_live_resume_report,
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
        "runtime_profile": "standard",
        "api_dialect": "responses",
        "structured_output_mode": "native_json_schema",
        "reasoning_effort": "high",
        "embedding_profile": "fixture",
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
        config=SimpleNamespace(
            runtime_profile="standard",
            llm=SimpleNamespace(
                provider="codex",
                model="contract-model",
                api_dialect="responses",
                structured_output_mode="native_json_schema",
                reasoning_effort="high",
            ),
            embedding=SimpleNamespace(profile_id="fixture"),
        ),
    )


def _packet(blind_item_id: str) -> ComparisonJudgePacket:
    return ComparisonJudgePacket(
        blind_item_id=blind_item_id,
        rubric_version="attribution-v2",
        criteria=(),
        response=AttributionBlindItem(
            blind_case_id=blind_item_id,
            conversation_history=(),
            question="fixture question",
            observed_outcome="runtime_failure",
            conclusion=None,
            hypotheses=(),
            evidence=(),
            requested_data=(),
        ),
    )


def _result_for_slot(slot: Any, *, packet: bool = True) -> ArchitectureRunResult:
    blind_item_id = f"blind_{slot.position:016x}"
    return ArchitectureRunResult(
        architecture=slot.architecture,
        case_id=slot.case_id,
        repetition=slot.repetition,
        blind_item_id=blind_item_id,
        quality_score=0.5,
        model_turns=1,
        tool_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd=0.000005,
        latency_ms=1,
        judge_packet=_packet(blind_item_id) if packet else None,
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
        return _result_for_slot(slot)

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
    assert report.quality_basis == "structural_proxy"
    assert report.conclusion == "pending_human_review"
    assert "not a Live quality" in report.applicability_boundary


@pytest.mark.asyncio
async def test_live_budget_ledger_rejects_usage_above_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def oversized_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        slot = args[0]
        return _result_for_slot(slot).model_copy(
            update={"input_tokens": 101, "output_tokens": 1, "total_tokens": 102}
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
            blind_secret=b"c" * 32,
        )


@pytest.mark.asyncio
async def test_live_resume_requires_exact_execution_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        return _result_for_slot(args[0])

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    first = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(object()),
        target=_target(),
        data_dir=tmp_path / "first",
        pricing_snapshot=_pricing(),
        max_new_case_runs=2,
        blind_secret=b"c" * 32,
    )
    assert len(first.runs) == 2

    with pytest.raises(ComparisonError, match="complete prefix"):
        await run_comparison_live(
            MANIFEST,
            rubric_path=RUBRIC,
            base_runtime=_runtime(object()),
            target=_target(),
            data_dir=tmp_path / "hole",
            pricing_snapshot=_pricing(),
            resume_runs=(first.runs[1],),
            max_new_case_runs=1,
            blind_secret=b"c" * 32,
        )
    with pytest.raises(ComparisonError, match="complete prefix"):
        await run_comparison_live(
            MANIFEST,
            rubric_path=RUBRIC,
            base_runtime=_runtime(object()),
            target=_target(),
            data_dir=tmp_path / "reversed",
            pricing_snapshot=_pricing(),
            resume_runs=tuple(reversed(first.runs)),
            max_new_case_runs=1,
            blind_secret=b"c" * 32,
        )


@pytest.mark.asyncio
async def test_live_resume_rejects_missing_judge_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        return _result_for_slot(args[0])

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    first = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(object()),
        target=_target(),
        data_dir=tmp_path / "first",
        pricing_snapshot=_pricing(),
        max_new_case_runs=1,
        blind_secret=b"c" * 32,
    )
    missing_packet = first.runs[0].model_copy(update={"judge_packet": None})

    with pytest.raises(ComparisonError, match="judge packet"):
        await run_comparison_live(
            MANIFEST,
            rubric_path=RUBRIC,
            base_runtime=_runtime(object()),
            target=_target(),
            data_dir=tmp_path / "missing",
            pricing_snapshot=_pricing(),
            resume_runs=(missing_packet,),
            max_new_case_runs=1,
            blind_secret=b"c" * 32,
        )


@pytest.mark.asyncio
async def test_frozen_binding_rejects_same_id_with_changed_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        return _result_for_slot(args[0])

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    report = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(object()),
        target=_target(),
        data_dir=tmp_path,
        pricing_snapshot=_pricing(),
        max_new_case_runs=1,
        blind_secret=b"c" * 32,
    )

    with pytest.raises(ComparisonError, match="frozen target"):
        validate_comparison_live_resume_report(
            report,
            manifest_path=MANIFEST,
            rubric_path=RUBRIC,
            target=_target(rate_tier="off_peak"),
            pricing_snapshot=_pricing(),
            judge_basis="structural_proxy",
        )
    with pytest.raises(ComparisonError, match="frozen target"):
        validate_comparison_live_resume_report(
            report,
            manifest_path=MANIFEST,
            rubric_path=RUBRIC,
            target=_target(reasoning_effort="medium"),
            pricing_snapshot=_pricing(),
            judge_basis="structural_proxy",
        )

    for field, changed in (
        ("tool_profile", "changed-tools"),
        ("termination_rule", "changed-termination"),
    ):
        changed_config = report.config.model_copy(update={field: changed})
        changed_report = report.model_copy(update={"config": changed_config})
        with pytest.raises(ComparisonError, match="frozen target"):
            validate_comparison_live_resume_report(
                changed_report,
                manifest_path=MANIFEST,
                rubric_path=RUBRIC,
                target=_target(),
                pricing_snapshot=_pricing(),
                judge_basis="structural_proxy",
            )

    changed_prices = TokenPrices(
        input_cache_hit_per_million_usd=0.1,
        input_cache_miss_per_million_usd=0.3,
        output_per_million_usd=0.4,
        reasoning_per_million_usd=0.4,
    )
    changed_pricing = _pricing().model_copy(
        update={
            "models": {
                "contract-model": TieredModelPrices(
                    peak=changed_prices,
                    off_peak=changed_prices,
                )
            }
        }
    )
    with pytest.raises(ComparisonError, match="frozen target"):
        validate_comparison_live_resume_report(
            report,
            manifest_path=MANIFEST,
            rubric_path=RUBRIC,
            target=_target(),
            pricing_snapshot=changed_pricing,
            judge_basis="structural_proxy",
        )


@pytest.mark.asyncio
async def test_blind_ids_use_run_secret_and_review_order_is_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = bytes(range(32))

    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        slot = args[0]
        blind_item_id = derive_comparison_blind_item_id(secret, slot)
        return _result_for_slot(slot).model_copy(
            update={
                "blind_item_id": blind_item_id,
                "judge_packet": _packet(blind_item_id),
            }
        )

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    report = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(object()),
        target=_target(),
        data_dir=tmp_path,
        pricing_snapshot=_pricing(),
        max_new_case_runs=6,
        blind_secret=secret,
    )
    first_slot = report.execution_order[0]
    old_digest = hashlib.sha256(
        (
            f"{report.rubric.preregistration_sha256}:{first_slot.position}:{first_slot.case_id}"
        ).encode()
    ).hexdigest()[:16]
    assert report.runs[0].blind_item_id != f"blind_{old_digest}"

    packets = shuffled_blind_review_packets(report, blind_secret=secret)
    packet_ids = [packet.blind_item_id for packet in packets]
    assert packet_ids != [run.blind_item_id for run in report.runs]
    assert set(packet_ids) == {run.blind_item_id for run in report.runs}


@pytest.mark.asyncio
async def test_wrapped_structural_proxy_cannot_become_external_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        return _result_for_slot(args[0])

    async def wrapped_fixture(packet: ComparisonJudgePacket) -> float:
        return await compare.fixture_judge(packet)

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    report = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=_runtime(object()),
        target=_target(),
        data_dir=tmp_path,
        pricing_snapshot=_pricing(),
        judge=wrapped_fixture,
        judge_basis="structural_proxy",
        blind_secret=b"c" * 32,
    )

    assert report.quality_basis == "structural_proxy"
    assert report.conclusion == "pending_human_review"
    assert "not a Live quality" in report.applicability_boundary


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
