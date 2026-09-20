"""Runner failures must retain previously completed Live evidence."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from oria.eval import compare
from oria.eval.attribution import AttributionBlindItem
from oria.eval.compare import (
    ArchitectureBudgets,
    ArchitectureRunResult,
    ComparisonConfig,
    ComparisonExecutionSlot,
    ComparisonJudgePacket,
    ComparisonLiveBudget,
    ComparisonLiveConfig,
    ComparisonLiveTarget,
    ComparisonReport,
    ComparisonRubric,
    RubricMetric,
    run_comparison_live,
)
from oria.eval.nightly import PricingSnapshot, TieredModelPrices, TokenPrices

pytestmark = pytest.mark.contract
RUNNER = Path(__file__).resolve().parents[2] / "scripts/run_comparison_live.py"
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "eval/datasets/scenario_b/v2.manifest.json"
RUBRIC = ROOT / "eval/config/attribution-rubric-v2.yaml"


def _live_target() -> ComparisonLiveTarget:
    return ComparisonLiveTarget(
        target_id="codex-contract",
        provider="codex",
        model="contract-model",
        runtime_profile="standard",
        api_dialect="responses",
        structured_output_mode="native_json_schema",
        reasoning_effort="high",
        embedding_profile="fixture",
        credential_env="ORIA_CONTRACT_AUTH",
        pricing_snapshot_id="contract-pricing-v1",
        rate_tier="peak",
        repetitions=1,
        order_seed="contract-order",
        budget=ComparisonLiveBudget(
            max_cases=20,
            max_model_requests=160,
            max_input_tokens=2_000,
            max_output_tokens=1_000,
            max_cost_usd=20,
            max_wall_seconds=300,
            per_case_max_model_turns=8,
            per_case_max_tool_calls=10,
            per_case_max_input_tokens=100,
            per_case_max_output_tokens=50,
            per_case_max_cost_usd=1,
            per_case_timeout_seconds=30,
        ),
    )


def _live_pricing() -> PricingSnapshot:
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
        source_url="https://oria.invalid/runner-contract",
        verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2036, 1, 1, tzinfo=UTC),
        models={"contract-model": TieredModelPrices(peak=prices, off_peak=prices)},
    )


def _run_result(slot: ComparisonExecutionSlot) -> ArchitectureRunResult:
    blind_item_id = f"blind_{slot.position:016x}"
    packet = ComparisonJudgePacket(
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
    return ArchitectureRunResult(
        architecture=slot.architecture,
        case_id=slot.case_id,
        repetition=slot.repetition,
        blind_item_id=blind_item_id,
        quality_score=0,
        model_turns=1,
        tool_calls=0,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd=0,
        latency_ms=1,
        judge_packet=packet,
    )


def _empty_live_report() -> ComparisonReport:
    budget = ComparisonLiveBudget(
        max_cases=1,
        max_model_requests=8,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_usd=1,
        max_wall_seconds=30,
        per_case_max_model_turns=8,
        per_case_max_tool_calls=10,
        per_case_max_input_tokens=100,
        per_case_max_output_tokens=50,
        per_case_max_cost_usd=1,
        per_case_timeout_seconds=30,
    )
    rubric = ComparisonRubric(
        rubric_version="contract-v1",
        rubric_sha256="1" * 64,
        judge_criteria=(),
        metrics=(
            RubricMetric(metric_id="quality", category="quality", higher_is_better=True),
            RubricMetric(metric_id="cost", category="cost", higher_is_better=False),
            RubricMetric(metric_id="latency", category="latency", higher_is_better=False),
            RubricMetric(metric_id="variance", category="variance", higher_is_better=False),
        ),
        preregistration_sha256="2" * 64,
    )
    return ComparisonReport(
        verification_level="live",
        run_status="in_progress",
        target_id="contract-target",
        dataset_version="contract-v1",
        dataset_sha256="3" * 64,
        pricing_snapshot_id="contract-pricing-v1",
        config=ComparisonConfig(
            seed="contract-seed",
            repetitions=1,
            model_id="contract-model",
            budgets=ArchitectureBudgets(single=budget, multi=budget),
        ),
        rubric=rubric,
        execution_order=(
            ComparisonExecutionSlot(
                position=0,
                architecture="single",
                case_id="case-1",
                repetition=1,
            ),
            ComparisonExecutionSlot(
                position=1,
                architecture="multi",
                case_id="case-1",
                repetition=1,
            ),
        ),
        runs=(),
        single=None,
        multi=None,
        delta_multi_minus_single=None,
        conclusion=None,
        applicability_boundary="Incomplete contract report.",
    )


def test_failure_preserves_existing_report(tmp_path: Path) -> None:
    runner = runpy.run_path(str(RUNNER))
    output = tmp_path / "run.json"
    output.write_text('{"evidence": "completed run"}\n', encoding="utf-8")
    original = output.read_bytes()
    runner["_record_failure"](output, {"status": "failed"})
    runner["_record_failure"](output, {"status": "failed"})
    assert output.read_bytes() == original
    assert len(list(tmp_path.glob("run.failure-*.json"))) == 2


@pytest.mark.asyncio
async def test_live_requires_explicit_switch_before_preflight() -> None:
    runner = runpy.run_path(str(RUNNER))
    with pytest.raises(runner["ComparisonError"], match="requires --run-live"):
        await runner["_run"](SimpleNamespace(preflight_only=False, run_live=False))


@pytest.mark.asyncio
async def test_existing_evidence_cannot_be_overwritten(tmp_path: Path) -> None:
    runner = runpy.run_path(str(RUNNER))
    output = tmp_path / "run.json"
    output.write_text("prior evidence\n", encoding="utf-8")
    with pytest.raises(runner["ComparisonError"], match="output already exists"):
        await runner["_run"](
            SimpleNamespace(preflight_only=False, run_live=True, output=output, resume=False)
        )
    assert output.read_text(encoding="utf-8") == "prior evidence\n"


def test_in_flight_reservation_blocks_resume_with_unknown_request_count() -> None:
    runner = runpy.run_path(str(RUNNER))
    reservation = runner["ComparisonInFlight"](
        position=29,
        architecture="multi",
        case_id="sb-v2-034",
        repetition=2,
        reserved_at=datetime.now(UTC),
    )

    with pytest.raises(runner["InFlightReservationError"]) as raised:
        runner["_assert_no_in_flight"](reservation)

    assert raised.value.evidence["request_count"] == "unknown"
    assert raised.value.evidence["in_flight"]["position"] == 29


def test_slot_reservation_is_persisted_before_an_exception(tmp_path: Path) -> None:
    runner = runpy.run_path(str(RUNNER))
    slot = _empty_live_report().execution_order[0]
    state = runner["ComparisonRunState"].model_construct(
        schema_version=1,
        target_id="contract-target",
        blind_secret_hex="01" * 32,
        frozen_binding=None,
        judge_basis="structural_proxy",
        created_at=datetime.now(UTC),
        in_flight=None,
        pending_results=(),
    )
    state_path = tmp_path / ".run.state.json"

    reserved = runner["_reserve_run_state"](state_path, state, slot)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))

    assert persisted["in_flight"]["position"] == slot.position
    assert persisted["in_flight"]["request_count"] == "unknown"
    with pytest.raises(runner["InFlightReservationError"]):
        runner["_assert_no_in_flight"](reserved.in_flight)


def test_second_process_cannot_acquire_same_run_lock(tmp_path: Path) -> None:
    runner = runpy.run_path(str(RUNNER))
    output = tmp_path / "run.json"
    lock_path = runner["_lock_path"](output)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                "p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); "
                "f=p.open('a+'); fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); time.sleep(30)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with (
            pytest.raises(runner["ComparisonError"], match="another comparison Live process"),
            runner["_exclusive_run_lock"](output),
        ):
            pass
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_sidecar_stage_failure_does_not_publish_new_main_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = runpy.run_path(str(RUNNER))
    output = tmp_path / "run.json"
    output.write_text('{"evidence": "old"}\n', encoding="utf-8")
    original = output.read_bytes()
    publish = runner["_publish_report_bundle"]
    original_stage = publish.__globals__["_stage_bytes"]

    def fail_sidecar(path: Path, payload: bytes, *, mode: int) -> Path:
        if "blind-review" in path.name:
            raise OSError("injected sidecar stage failure")
        return original_stage(path, payload, mode=mode)

    monkeypatch.setitem(publish.__globals__, "_stage_bytes", fail_sidecar)
    with pytest.raises(OSError, match="sidecar stage failure"):
        publish(output, _empty_live_report(), blind_secret=bytes(range(32)))

    assert output.read_bytes() == original


@pytest.mark.asyncio
async def test_completed_resume_path_rebuilds_and_hash_validates_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = runpy.run_path(str(RUNNER))
    output = tmp_path / "run.json"
    target = _live_target()
    pricing = _live_pricing()

    async def fake_slot(*args: Any, **kwargs: Any) -> ArchitectureRunResult:
        return _run_result(args[0])

    monkeypatch.setattr(compare, "run_architecture_slot", fake_slot)
    report = await run_comparison_live(
        MANIFEST,
        rubric_path=RUBRIC,
        base_runtime=SimpleNamespace(
            llm=object(),
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
        ),
        target=target,
        data_dir=tmp_path / "fixture",
        pricing_snapshot=pricing,
        blind_secret=b"o" * 32,
    )
    assert report.run_status == "completed"
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    config_path = tmp_path / "comparison-live.yaml"
    config_path.write_text(
        yaml.safe_dump(
            ComparisonLiveConfig(targets=(target,)).model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    (pricing_dir / "contract-pricing-v1.yaml").write_text(
        yaml.safe_dump(pricing.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    sidecar = runner["_blind_review_path"](output)
    assert not sidecar.exists()
    run_globals = runner["_run"].__globals__

    def forbidden_runtime(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("completed resume must not construct a Live runtime")

    monkeypatch.setitem(run_globals, "resolve_runtime_config", forbidden_runtime)
    result = await runner["_run"](
        SimpleNamespace(
            target=target.target_id,
            config=config_path,
            pricing_dir=pricing_dir,
            manifest=MANIFEST,
            rubric=RUBRIC,
            data_dir=tmp_path / "run-data",
            output=output,
            preflight_only=False,
            run_live=True,
            resume=True,
            max_new_case_runs=1,
        )
    )

    assert result == 0
    payload = sidecar.read_bytes()
    repaired = ComparisonReport.model_validate_json(output.read_text(encoding="utf-8"))

    assert len(json.loads(payload)) == 40
    assert b"blind_secret" not in payload
    assert (
        bytes.fromhex(
            json.loads(runner["_state_path"](output).read_text(encoding="utf-8"))[
                "blind_secret_hex"
            ]
        )
        not in payload
    )
    assert repaired.blind_review_sha256 == hashlib.sha256(payload).hexdigest()
    assert repaired.run_status == "completed"
