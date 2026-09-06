"""Deterministic end-to-end evaluation coverage for an approved Scenario B fixture."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import Principal
from oria.data import initialize_data
from oria.eval import (
    assert_attribution_gates,
    create_attribution_baseline,
    load_attribution_gates,
    run_attribution_eval,
)
from oria.eval.attribution import build_attribution_eval_runtime
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset

pytestmark = pytest.mark.integration


def _approved_fixture(root: Path, destination: Path) -> tuple[Path, Path]:
    source_dataset = root / "eval/datasets/scenario_b/v1.jsonl"
    source_manifest = root / "eval/datasets/scenario_b/manifest.json"
    source_rubric = root / "eval/config/attribution-rubric-v1.yaml"
    dataset_dir = destination / "eval/datasets/scenario_b"
    config_dir = destination / "eval/config"
    dataset_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    rubric_path = config_dir / source_rubric.name
    shutil.copyfile(source_rubric, rubric_path)

    reviewed_at = datetime(2026, 9, 4, 21, 30).astimezone().isoformat()
    cases = []
    for line in source_dataset.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        case["review"] = {
            "status": "approved",
            "reviewed_by": "fixture-human-reviewer",
            "reviewed_at": reviewed_at,
        }
        cases.append(case)
    payload = "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n"
    dataset_path = dataset_dir / "v1.jsonl"
    dataset_path.write_text(payload, encoding="utf-8")

    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    manifest.update(
        {
            "dataset_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "review_status": "approved",
            "human_review_complete": True,
            "holdout_frozen": True,
            "rubric_sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
        }
    )
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path, rubric_path


@pytest.mark.asyncio
async def test_approved_holdout_runs_real_graph_and_emits_blind_packet(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest, rubric = _approved_fixture(root, tmp_path / "assets")

    report = await run_attribution_eval(
        manifest,
        rubric_path=rubric,
        data_dir=tmp_path / "run",
        split="holdout",
    )

    assert report.suite == "attribution"
    assert report.verification_level == "fixture"
    assert report.metrics.evaluated_cases == 20
    assert report.metrics.case_pass_rate == 1.0
    assert report.metrics.critical_pass_rate == 1.0
    assert report.metrics.grounded_evidence_rate == 1.0
    assert len(report.blind_review_items) == 20
    blind_payload = report.blind_review_items[0].model_dump(mode="json")
    assert "case_id" not in blind_payload
    assert "split" not in blind_payload
    assert "critical" not in blind_payload
    assert "golden_rationale" not in blind_payload
    assert "provider_profile" not in blind_payload
    history_items = [item for item in report.blind_review_items if item.conversation_history]
    assert len(history_items) == 6
    assert all(
        tuple(message.role for message in item.conversation_history) == ("user", "assistant")
        for item in history_items
    )

    gates = load_attribution_gates(root / "eval/config/attribution-gates.yaml")
    assert_attribution_gates(report, gates=gates)


@pytest.mark.asyncio
async def test_complete_approved_fixture_can_create_a_frozen_baseline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest, rubric = _approved_fixture(root, tmp_path / "assets")
    report = await run_attribution_eval(
        manifest,
        rubric_path=rubric,
        data_dir=tmp_path / "run",
        split="all",
    )

    baseline = create_attribution_baseline(
        report,
        created_at=datetime(2026, 9, 4, 22, 0).astimezone(),
    )
    gates = load_attribution_gates(root / "eval/config/attribution-gates.yaml")
    assert_attribution_gates(report, gates=gates, baseline=baseline)

    assert baseline.split == "all"
    assert len(baseline.cases) == 50
    assert baseline.metrics.case_pass_rate == 1.0


@pytest.mark.asyncio
async def test_scoped_live_identities_are_trusted_by_attribution_tools(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = load_golden_dataset(root / "eval/datasets/scenario_b/manifest.json")
    case = next(
        item
        for item in dataset.cases
        if isinstance(item, AttributionGoldenCase) and item.split == "holdout"
    )
    query_database = tmp_path / "analytics.db"
    generate_attribution_fixture(query_database, tmp_path / "labels.db")
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    base = await build_runtime(config)
    actor = Principal(
        subject_id="live-reviewer",
        tenant_id=case.tenant_id,
        kind="human",
        roles=("operator",),
        authn_method="trusted-live-eval",
    )
    executor = Principal(
        subject_id="live-runner",
        tenant_id=case.tenant_id,
        kind="service",
        roles=("runtime",),
        authn_method="trusted-live-eval",
    )
    scoped = build_attribution_eval_runtime(
        base,
        (case,),
        query_database,
        trusted_actors=(actor,),
        trusted_executors=(executor,),
    )
    try:
        ctx = scoped.new_context(
            actor=actor,
            executor=executor,
            session_id="live-test",
            thread_id="live-test",
            run_id="live-test",
        )
        await scoped.tools.preflight(
            "query_funnel",
            {
                "period": {"start_date": "2026-08-18", "end_date": "2026-09-01"},
                "dimensions": ["event_date"],
                "region": "east",
                "category": "full_service",
            },
            ctx,
        )
    finally:
        await scoped.aclose()
        await base.aclose()
