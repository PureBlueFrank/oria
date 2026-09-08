"""Run approved Scenario B V2 Golden cases and enforce the frozen Fixture baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from oria.eval import (
    AttributionEvalError,
    assert_attribution_gates,
    create_attribution_baseline,
    load_attribution_baseline,
    load_attribution_gates,
    run_attribution_eval,
    write_value_model,
)


async def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "eval/datasets/scenario_b/v2.manifest.json"
    rubric = root / "eval/config/attribution-rubric-v2.yaml"
    gates = load_attribution_gates(root / "eval/config/attribution-gates-v2.yaml")
    baseline_path = root / "eval/baselines/attribution/2.json"
    output_path = Path(args.output)
    with tempfile.TemporaryDirectory(prefix="oria-attribution-golden-v2-") as temporary:
        report = await run_attribution_eval(
            manifest,
            rubric_path=rubric,
            data_dir=Path(temporary) / "data",
            split="all",
        )
    if args.create_baseline:
        if baseline_path.exists():
            raise AttributionEvalError("refusing to overwrite an attribution baseline")
        assert_attribution_gates(report, gates=gates)
        baseline = create_attribution_baseline(
            report,
            created_at=datetime.now().astimezone(),
        )
        write_value_model(baseline_path, baseline)
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_value["baseline_created"] = True
        manifest.write_text(
            json.dumps(manifest_value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        baseline = load_attribution_baseline(baseline_path)
        assert_attribution_gates(report, gates=gates, baseline=baseline)
    write_value_model(output_path, report)
    print(
        json.dumps(
            {
                "ok": True,
                "suite": report.suite,
                "dataset_version": report.dataset_version,
                "case_count": len(report.cases),
                "passed_count": sum(case.passed for case in report.cases),
                "eval_fingerprint": report.eval_fingerprint,
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
        default=".artifacts/eval/attribution_v2.json",
        help="Path for the deterministic per-case report and blind-review packet.",
    )
    parser.add_argument(
        "--create-baseline",
        action="store_true",
        help="Create the first baseline; refuses to overwrite an existing file.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except (AttributionEvalError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
