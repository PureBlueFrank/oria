"""Validate the Scenario A Golden dataset without executing external providers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from oria.eval import HumanReviewRequired, load_golden_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-pending", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = root / "eval" / "datasets" / "scenario_a" / "v1.manifest.json"
    try:
        dataset = load_golden_dataset(
            manifest,
            require_human_review=not args.allow_pending,
        )
    except HumanReviewRequired as exc:
        print(json.dumps({"ok": False, "status": "blocked", "reason": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "status": dataset.manifest.review_status,
                "case_count": len(dataset.cases),
                "critical_case_count": sum(case.critical for case in dataset.cases),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
