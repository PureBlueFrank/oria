"""Verify T03 assets and data init from an installed wheel without source paths."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import oria
from oria.resources.loader import RULE_CATEGORIES, load_demo_data, verify_package_assets


def _run_init(data_dir: Path) -> dict[str, object]:
    executable = Path(sys.executable).parent / "oria"
    completed = subprocess.run(
        [str(executable), "data", "init", "--output", "json", "--data-dir", str(data_dir)],
        cwd=data_dir.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("data init did not return a JSON object")
    return payload


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    data_dir: Path = args.data_dir.resolve(strict=False)
    if data_dir.exists():
        raise AssertionError("wheel verification requires a fresh data directory")
    data_dir.parent.mkdir(parents=True, exist_ok=True)

    package_file = Path(oria.__file__).resolve()
    if "site-packages" not in package_file.parts:
        raise AssertionError("Oria was not imported from an installed wheel environment")
    manifest, heads = verify_package_assets()
    bundle = load_demo_data()
    if manifest.rule_categories != RULE_CATEGORIES or len(RULE_CATEGORIES) != 6:
        raise AssertionError("installed wheel does not contain the six-rule manifest")
    if heads != {"platform": "platform_0005", "business": "business_0007"}:
        raise AssertionError("installed wheel migration heads are incorrect")
    if len(bundle.merchants.merchants) != 12:
        raise AssertionError("installed wheel merchant resource is incomplete")

    first = _run_init(data_dir)
    second = _run_init(data_dir)
    first_data = first.get("data")
    second_data = second.get("data")
    if not isinstance(first_data, dict) or first_data.get("merchants_inserted") != 12:
        raise AssertionError("fresh installed-wheel initialization did not seed 12 merchants")
    if not isinstance(second_data, dict) or second_data.get("merchants_inserted") != 0:
        raise AssertionError("repeated installed-wheel initialization is not idempotent")

    platform_tables = _tables(data_dir / "sqlite" / "platform.db")
    business_tables = _tables(data_dir / "sqlite" / "business.db")
    if not {"documents", "document_versions", "ingestion_runs", "checkpoints", "writes"}.issubset(
        platform_tables
    ):
        raise AssertionError("platform migration or official saver setup is incomplete")
    if not {"merchants", "campaigns", "coupon_batches", "enrollment_items"}.issubset(
        business_tables
    ):
        raise AssertionError("installed business migration chain is incomplete")
    print(
        "verified installed wheel assets, current revisions, idempotent data init, "
        f"and V0.3 business tables from {package_file}"
    )


if __name__ == "__main__":
    main()
