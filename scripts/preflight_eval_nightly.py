"""Validate a selected nightly target before any external request can be sent."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from oria.eval import preflight_nightly_target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--config", type=Path, default=Path("eval/config/nightly.yaml"))
    parser.add_argument("--pricing-dir", type=Path, default=Path("eval/config/pricing"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/nightly-preflight.json"),
    )
    args = parser.parse_args()
    result = preflight_nightly_target(
        config_path=args.config,
        pricing_dir=args.pricing_dir,
        target_id=args.target,
        environ=os.environ,
        now=datetime.now().astimezone(),
        known_targets=frozenset({"deepseek", "kimi", "zhipu", "openai", "anthropic"}),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
