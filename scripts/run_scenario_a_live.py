"""Explicitly opt-in to Scenario A Live; never collected by community pytest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from oria.eval.scenario_a_live import ScenarioALiveReport, run_scenario_a_live


def _write_report(path: Path, report: ScenarioALiveReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _run(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError("output already exists; select a new output path")
    manifest = Path(__file__).resolve().parents[1] / "eval/datasets/scenario_a/v1.manifest.json"
    with tempfile.TemporaryDirectory(prefix="oria-scenario-a-live-") as temporary:
        report = await run_scenario_a_live(
            manifest,
            target=args.target,
            data_dir=Path(temporary) / "data",
            environ=dict(os.environ),
            max_new_case_runs=args.max_new_case_runs,
            checkpoint=lambda report: _write_report(args.output, report),
        )
    print(
        json.dumps(
            {
                "status": report.status,
                "target_id": report.target_id,
                "case_count": len(report.cases),
                "passed_count": sum(case.automated_pass for case in report.cases),
                "metrics": report.metrics.model_dump() if report.metrics is not None else None,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 2 if report.status == "failed" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/eval/scenario-a-live/run.json")
    )
    parser.add_argument(
        "--max-new-case-runs", type=int, help="Run the first N cases; no resume yet"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
