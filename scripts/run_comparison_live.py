"""Run the frozen single/multi holdout against one explicitly selected Live target."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ValueModel
from oria.data import initialize_data
from oria.eval.compare import (
    ComparisonError,
    ComparisonReport,
    load_comparison_live_config,
    load_comparison_pricing_snapshot,
    preflight_comparison_live,
    run_comparison_live,
    select_comparison_live_target,
)

_CODEX_SUBSCRIPTION_TARGET = "codex-subscription-gpt56-sol-high"
_KNOWN_TARGETS = frozenset({"deepseek", "deepseek-pro-structured", _CODEX_SUBSCRIPTION_TARGET})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--config", type=Path, default=Path("eval/config/comparison-live-v1.yaml"))
    parser.add_argument("--pricing-dir", type=Path, default=Path("eval/config/pricing"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/datasets/scenario_b/v2.manifest.json"),
    )
    parser.add_argument(
        "--rubric",
        type=Path,
        default=Path("eval/config/attribution-rubric-v2.yaml"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path(".artifacts/eval/comparison-live-v1"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/comparison-live-v1/run.json"),
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume completed architecture runs from the existing output report",
    )
    parser.add_argument(
        "--max-new-case-runs",
        type=int,
        help="pause cleanly after this many newly completed architecture runs",
    )
    return parser.parse_args()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, ValueModel):
        payload = value.model_dump_json(indent=2)
    else:
        payload = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")


def _codex_chatgpt_auth_ready() -> bool:
    try:
        completed = subprocess.run(
            ["codex", "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = completed.stdout + completed.stderr
    return completed.returncode == 0 and "Logged in using ChatGPT" in output


async def _run(args: argparse.Namespace) -> int:
    started_at = datetime.now().astimezone()
    resume_report: ComparisonReport | None = None
    if args.resume and args.output.exists():
        try:
            resume_report = ComparisonReport.model_validate_json(
                args.output.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ComparisonError("existing comparison Live report cannot be resumed") from exc
        if resume_report.target_id != args.target:
            raise ComparisonError("existing comparison report target does not match --target")
        if resume_report.run_status == "completed":
            print(resume_report.model_dump_json())
            return 0

    run_data_dir = args.data_dir / "runs" / started_at.strftime("%Y%m%dT%H%M%S%f%z")
    live_environ = dict(os.environ)
    if args.target == _CODEX_SUBSCRIPTION_TARGET and _codex_chatgpt_auth_ready():
        live_environ["ORIA_CODEX_CHATGPT_AUTH_READY"] = "1"
    preflight = preflight_comparison_live(
        config_path=args.config,
        manifest_path=args.manifest,
        rubric_path=args.rubric,
        pricing_dir=args.pricing_dir,
        target_id=args.target,
        environ=live_environ,
        now=started_at,
        known_targets=_KNOWN_TARGETS,
    )
    if preflight.status == "blocked" or args.preflight_only:
        _write_json(args.output, preflight)
        print(preflight.model_dump_json())
        return 0 if preflight.status == "ready" else 2

    config = load_comparison_live_config(args.config)
    target = select_comparison_live_target(config, args.target)
    snapshot = load_comparison_pricing_snapshot(
        args.pricing_dir / f"{target.pricing_snapshot_id}.yaml"
    )
    if resume_report is not None and (
        resume_report.verification_level != "live"
        or resume_report.config.model_id != target.model
        or resume_report.config.repetitions != target.repetitions
        or resume_report.config.seed != target.order_seed
        or resume_report.pricing_snapshot_id != target.pricing_snapshot_id
    ):
        raise ComparisonError("existing comparison report does not match the frozen target")

    runtime_environ = dict(live_environ)
    runtime_environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    resolved = resolve_runtime_config(
        runtime_profile="standard",
        llm_profile=target.target_id,
        embedding_profile="fixture",
        data_dir=run_data_dir / "runtime",
        environ=runtime_environ,
    )
    await initialize_data(resolved)
    runtime = await build_runtime(resolved)
    async with runtime:
        report = await run_comparison_live(
            args.manifest,
            rubric_path=args.rubric,
            base_runtime=runtime,
            target=target,
            data_dir=run_data_dir,
            pricing_snapshot=snapshot,
            resume_runs=() if resume_report is None else resume_report.runs,
            max_new_case_runs=args.max_new_case_runs,
        )
    _write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report.run_status,
                "target_id": report.target_id,
                "completed_architecture_runs": len(report.runs),
                "conclusion": report.conclusion,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    args = _arguments()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        failed = {
            "schema_version": 1,
            "suite": "attribution_comparison",
            "target_id": args.target,
            "status": "failed",
            "request_count": None,
            "reason": str(exc) if isinstance(exc, ComparisonError) else type(exc).__name__,
        }
        _write_json(args.output, failed)
        print(json.dumps(failed, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
