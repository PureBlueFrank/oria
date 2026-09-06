"""Run the frozen Scenario B holdout against one explicitly selected Live target."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ValueModel
from oria.data import initialize_data
from oria.eval.attribution_live import (
    AttributionLiveError,
    load_attribution_live_config,
    preflight_attribution_live,
    run_attribution_live,
    select_attribution_live_target,
)


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
    run_data_dir = args.data_dir / "runs" / started_at.strftime("%Y%m%dT%H%M%S%f%z")
    preflight = preflight_attribution_live(
        config_path=args.config,
        pricing_dir=args.pricing_dir,
        target_id=args.target,
        environ=os.environ,
        now=started_at,
        known_targets=frozenset({"deepseek"}),
    )
    if preflight.status == "blocked" or args.preflight_only:
        _write_json(args.output, preflight)
        print(preflight.model_dump_json())
        return 0 if preflight.status == "ready" else 2

    config = load_attribution_live_config(args.config)
    target = select_attribution_live_target(config, args.target)
    runtime_environ = dict(os.environ)
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
            started_at=started_at,
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
    return 0 if report.status == "completed_pending_human_review" else 2


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
