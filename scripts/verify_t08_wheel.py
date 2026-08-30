"""Run the installed-wheel T08 CLI twice from a source-free fresh directory."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import oria

_EXPECTED_IDS = {
    "demo-m001",
    "demo-m002",
    "demo-m005",
    "demo-m006",
    "demo-m007",
    "demo-m008",
    "demo-m009",
    "demo-m010",
    "demo-m011",
    "demo-m012",
}
_RULE_CATEGORIES = {
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
}


def _run(work_dir: Path, data_dir: Path) -> dict[str, Any]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ORIA_")
        and name not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "oria",
            "demo",
            "--output",
            "json",
            "--data-dir",
            str(data_dir),
        ],
        cwd=work_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stderr:
        raise AssertionError("installed demo emitted unexpected stderr")
    value: Any = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AssertionError("installed demo output is not a JSON object")
    return value


def _assert_result(value: dict[str, Any]) -> None:
    if value.get("ok") is not True or value.get("schema_version") != 1:
        raise AssertionError("installed demo result envelope is invalid")
    proposal = value.get("proposal")
    validation = value.get("validation")
    events = value.get("events")
    if not isinstance(proposal, dict) or not isinstance(validation, dict):
        raise AssertionError("installed demo proposal or validation is missing")
    if set(proposal.get("rules", {})) != _RULE_CATEGORIES:
        raise AssertionError("installed demo did not return all six rule categories")
    recommendations = proposal.get("recommended_merchants")
    if not isinstance(recommendations, list):
        raise AssertionError("installed demo recommendations are missing")
    if {item.get("merchant_id") for item in recommendations} != _EXPECTED_IDS:
        raise AssertionError("installed demo returned an invalid hard-eligible set")
    if proposal.get("unresolved_items") != [] or not proposal.get("field_evidence"):
        raise AssertionError("installed demo unresolved items or citations are invalid")
    if not proposal.get("campaign_preview") or not proposal.get("coupon_batch_preview"):
        raise AssertionError("installed demo proposal previews are missing")
    tool_events = [
        event.get("tool")
        for event in events or []
        if isinstance(event, dict) and event.get("type") == "tool_completed"
    ]
    if tool_events != ["search_campaign_rules", "query_merchants"]:
        raise AssertionError("installed demo tool events are invalid")
    correlation_id = value.get("correlation_id")
    run_id = value.get("run_id")
    if not all(
        isinstance(event, dict)
        and event.get("correlation_id") == correlation_id
        and event.get("run_id") == run_id
        for event in events or []
    ):
        raise AssertionError("installed demo event correlation is incomplete")
    if validation.get("business_side_effect_free") is not True:
        raise AssertionError("installed demo business side-effect check failed")
    report_path = value.get("report_path")
    if not isinstance(report_path, str) or not Path(report_path).is_file():
        raise AssertionError("installed demo did not write its validation report")


def _business_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    package_file = Path(oria.__file__).resolve()
    if "site-packages" not in package_file.parts:
        raise AssertionError("T08 verifier must import Oria from an installed wheel")
    work_dir = args.work_dir.resolve(strict=False)
    if work_dir.exists():
        raise AssertionError("T08 wheel verification requires a fresh work directory")
    work_dir.mkdir(parents=True)
    data_dir = work_dir / "data"
    first = _run(work_dir, data_dir)
    second = _run(work_dir, data_dir)
    _assert_result(first)
    _assert_result(second)
    if first["initialization"]["merchants_inserted"] != 12:
        raise AssertionError("installed demo did not initialize the fresh fixture")
    if second["initialization"]["merchants_inserted"] != 0:
        raise AssertionError("installed demo repeat initialization was not idempotent")
    if first["run_id"] == second["run_id"] or first["correlation_id"] == second["correlation_id"]:
        raise AssertionError("installed demo repeated execution metadata")
    business_tables = _business_tables(data_dir / "sqlite" / "business.db")
    if not {"campaigns", "coupon_batches", "enrollment_items"}.issubset(business_tables):
        raise AssertionError("installed demo is missing the current business schema")
    print(
        json.dumps(
            {
                "ok": True,
                "package_file": str(package_file),
                "runs": 2,
                "eligible_merchants": 10,
                "tool_events": ["search_campaign_rules", "query_merchants"],
                "business_side_effect_free": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
