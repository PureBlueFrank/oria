"""Run the frozen Scenario B holdout against one explicitly selected Live target."""

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
from oria.eval.attribution_live import (
    AttributionLiveError,
    AttributionLiveRunReport,
    load_attribution_live_config,
    preflight_attribution_live,
    run_attribution_live,
    select_attribution_live_target,
)

_CODEX_SUBSCRIPTION_TARGET = "codex-subscription-gpt56-sol-high"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("eval/config/attribution-live-v1.yaml"),
    )
    parser.add_argument("--pricing-dir", type=Path, default=Path("eval/config/pricing"))
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".artifacts/eval/attribution-live-v1"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/attribution-live-v1/run.json"),
    )
    parser.add_argument(
        "--blind-output",
        type=Path,
        default=Path(".artifacts/eval/attribution-live-v1/blind-review.json"),
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume completed case repetitions from the existing output report",
    )
    parser.add_argument(
        "--max-new-case-runs",
        type=int,
        help="pause cleanly after this many newly completed case repetitions",
    )
    return parser.parse_args()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, ValueModel):
        payload = value.model_dump_json(indent=2)
    else:
        payload = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    started_at = datetime.now().astimezone()
    resume_report: AttributionLiveRunReport | None = None
    if args.resume and args.output.exists():
        try:
            resume_report = AttributionLiveRunReport.model_validate_json(
                args.output.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise AttributionLiveError("existing Live report cannot be resumed") from exc
        if resume_report.target_id != args.target:
            raise AttributionLiveError("existing Live report target does not match --target")
        if resume_report.status == "completed_pending_human_review":
            print(resume_report.model_dump_json())
            return 0
    run_data_dir = args.data_dir / "runs" / started_at.strftime("%Y%m%dT%H%M%S%f%z")
    live_environ = dict(os.environ)
    if args.target == _CODEX_SUBSCRIPTION_TARGET and _codex_chatgpt_auth_ready():
        live_environ["ORIA_CODEX_CHATGPT_AUTH_READY"] = "1"
    preflight = preflight_attribution_live(
        config_path=args.config,
        pricing_dir=args.pricing_dir,
        target_id=args.target,
        environ=live_environ,
        now=started_at,
        known_targets=frozenset(
            {"deepseek", "deepseek-pro-structured", _CODEX_SUBSCRIPTION_TARGET}
        ),
    )
    if preflight.status == "blocked" or args.preflight_only:
        _write_json(args.output, preflight)
        print(preflight.model_dump_json())
        return 0 if preflight.status == "ready" else 2

    config = load_attribution_live_config(args.config)
    target = select_attribution_live_target(config, args.target)
    if resume_report is not None and (
        resume_report.provider != target.provider
        or resume_report.model != target.model
        or resume_report.dataset_version != target.dataset_version
        or resume_report.dataset_sha256 != target.dataset_sha256
        or resume_report.rubric_sha256 != target.rubric_sha256
        or resume_report.baseline_fingerprint != target.baseline_fingerprint
        or resume_report.pricing_snapshot_id != target.pricing_snapshot_id
    ):
        raise AttributionLiveError("existing Live report does not match the frozen target")
    runtime_environ = dict(live_environ)
    runtime_environ.update(
        {
            "ORIA_ENVIRONMENT": "test",
            "ORIA_EMBEDDING_PROFILE": "fixture",
        }
    )
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
        report, blind_packet = await run_attribution_live(
            config_path=args.config,
            pricing_dir=args.pricing_dir,
            target=target,
            base_runtime=runtime,
            data_dir=run_data_dir,
            started_at=resume_report.started_at if resume_report is not None else started_at,
            resume_records=() if resume_report is None else resume_report.cases,
            max_new_case_runs=args.max_new_case_runs,
        )
    _write_json(args.output, report)
    _write_json(args.blind_output, blind_packet)
    print(
        json.dumps(
            {
                "status": report.status,
                "target_id": report.target_id,
                "completed_case_runs": report.completed_case_runs,
                "model_requests": report.usage.model_requests,
                "cost_usd": report.usage.cost_usd,
                "human_calibration": report.human_calibration.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status != "failed" else 2


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


def main() -> int:
    args = _arguments()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        failed = {
            "schema_version": 1,
            "suite": "attribution",
            "target_id": args.target,
            "status": "failed",
            "request_count": None,
            "reason": str(exc) if isinstance(exc, AttributionLiveError) else type(exc).__name__,
        }
        _write_json(args.output, failed)
        print(json.dumps(failed, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
