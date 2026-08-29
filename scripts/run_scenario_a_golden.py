"""Run the approved Scenario A Golden suite and enforce its frozen baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from oria.eval import (
    GoldenGateError,
    assert_scenario_a_gates,
    create_scenario_a_baseline,
    load_scenario_a_baseline,
    load_scenario_a_gates,
    run_scenario_a_golden,
    write_value_model,
)


async def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "eval" / "datasets" / "scenario_a" / "v1.manifest.json"
    baseline_path = root / "eval" / "baselines" / "scenario_a" / "1.json"
    gates_path = root / "eval" / "config" / "gates.yaml"
    output_path = Path(args.output)
    with tempfile.TemporaryDirectory(prefix="oria-scenario-a-golden-") as temporary:
        report = await run_scenario_a_golden(manifest, data_dir=Path(temporary) / "data")
    gates = load_scenario_a_gates(gates_path)
    if args.create_baseline:
        if baseline_path.exists():
            raise GoldenGateError("refusing to overwrite an existing Scenario A baseline")
        assert_scenario_a_gates(report, gates=gates)
        baseline = create_scenario_a_baseline(
            report,
            created_at=datetime.now().astimezone(),
        )
        write_value_model(baseline_path, baseline)
    else:
        baseline = load_scenario_a_baseline(baseline_path)
        assert_scenario_a_gates(report, gates=gates, baseline=baseline)
    write_value_model(output_path, report)
    print(
        json.dumps(
            {
                "ok": True,
                "suite": report.suite,
                "dataset_version": report.dataset_version,
                "case_count": len(report.cases),
                "passed_count": sum(case.passed for case in report.cases),
                "metrics": report.metrics.model_dump(mode="json"),
                "output": str(output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=".artifacts/eval/scenario_a_v1.json",
        help="Path for the deterministic per-case report.",
    )
    parser.add_argument(
        "--create-baseline",
        action="store_true",
        help="Create the first baseline; refuses to overwrite an existing file.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except GoldenGateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
