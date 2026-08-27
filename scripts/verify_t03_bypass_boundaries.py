"""Adversarial T03 probe: verify prohibited outcomes cannot be reached by detours."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import shutil
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import DataInitializationError, initialize_data
from oria.domain.models import EligibilityCriteria
from oria.permission.local import local_cli_executor, local_operator
from oria.resources import loader
from oria.resources.loader import PackageAssetError, load_demo_data


def _config(path: Path):
    return resolve_runtime_config(environ={}, data_dir=path)


def _expect_asset_failure(action: object) -> None:
    if not callable(action):
        raise AssertionError("asset probe action is not callable")
    try:
        action()
    except PackageAssetError:
        return
    raise AssertionError("modified or missing package asset was accepted")


async def _expect_init_failure(config: object) -> None:
    try:
        await initialize_data(config)
    except DataInitializationError:
        return
    raise AssertionError("unsafe data initialization path was accepted")


async def _probe_runtime_and_policy(work_dir: Path) -> None:
    config = _config(work_dir / "runtime-data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="probe-session",
            thread_id="probe-thread",
            run_id="probe-run",
        )
        for name in ("repository", "repositories", "engine", "session", "session_factory"):
            if hasattr(ctx, name) or hasattr(runtime, name) or hasattr(ctx.domain, name):
                raise AssertionError(f"runtime exposed prohibited storage surface: {name}")
        parameters = inspect.signature(ctx.domain.merchants.eligible_merchants).parameters
        if tuple(parameters) != ("rule_set_id", "limit", "ctx"):
            raise AssertionError("merchant service accepts a hard-rule override parameter")
        try:
            await ctx.domain.merchants.eligible_merchants(
                "demo-east-dining-v1",
                10,
                ctx,
                filters={"ignore_denylist": True},
            )
        except TypeError:
            pass
        else:
            raise AssertionError("merchant service accepted a caller-supplied filter detour")
        result = await ctx.domain.merchants.eligible_merchants("demo-east-dining-v1", 100, ctx)
        candidate_ids = {merchant.merchant_id for merchant in result.merchants}
        if "demo-m004" in candidate_ids or "demo-m011" in candidate_ids:
            raise AssertionError("hard-ineligible merchant reached the candidate set")
        expected = {"demo-m001", "demo-m002", "demo-m009", "demo-m010", "demo-m012"}
        if candidate_ids != expected:
            raise AssertionError("deterministic candidate set changed")
    finally:
        await runtime.aclose()

    bundle = load_demo_data()
    public = repr(bundle.rules) + bundle.rules.model_dump_json()
    for restricted in ("demo-m004", "synthetic-east-a", "synthetic-east-b"):
        if restricted in public:
            raise AssertionError("restricted rule field leaked through repr or serialization")
    values = bundle.rules.internal_eligibility_criteria().model_dump()
    if {"allowlist_merchant_ids", "denylist_merchant_ids", "sales_org_scope"}.intersection(values):
        raise AssertionError("restricted criteria leaked through model_dump")
    criteria_payload = {
        "rule_set_id": "rules-v1",
        "rule_version": "1.0.0",
        "categories": (),
        "cities": ("上海",),
        "enrollment_systems": ("demo-enroll",),
        "allowlist_merchant_ids": ("merchant-a",),
        "denylist_merchant_ids": ("merchant-b",),
        "sales_org_scope": ("east-a",),
    }
    try:
        EligibilityCriteria.model_validate(criteria_payload)
    except ValidationError:
        pass
    else:
        raise AssertionError("empty hard-rule collection relaxed eligibility")


async def _probe_migrations_and_paths(work_dir: Path) -> None:
    repeat_config = _config(work_dir / "repeat-data")
    first = await initialize_data(repeat_config)
    second = await initialize_data(repeat_config)
    if first.merchants_inserted != 12 or second.merchants_inserted != 0:
        raise AssertionError("repeat initialization changed seed data")
    with sqlite3.connect(repeat_config.data_paths.business_db) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    if any("campaign" in table or "coupon" in table for table in tables):
        raise AssertionError("T03 initialization created Campaign/CouponBatch state")

    stamped_config = _config(work_dir / "stamped-data")
    stamped_config.data_paths.platform_db.parent.mkdir(parents=True)
    with sqlite3.connect(stamped_config.data_paths.platform_db) as connection:
        connection.execute("CREATE TABLE alembic_version_platform (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version_platform VALUES ('platform_0002')")
        connection.commit()
    await _expect_init_failure(stamped_config)
    if stamped_config.data_paths.business_db.exists():
        raise AssertionError("runner continued after a skipped platform schema")

    cross_config = _config(work_dir / "cross-chain-data")
    await initialize_data(cross_config)
    with sqlite3.connect(cross_config.data_paths.platform_db) as connection:
        connection.execute("UPDATE alembic_version_platform SET version_num = 'business_0001'")
        connection.commit()
    await _expect_init_failure(cross_config)

    symlink_config = _config(work_dir / "symlink-data")
    outside = work_dir / "outside-data"
    outside.mkdir()
    symlink_config.data_dir.mkdir()
    (symlink_config.data_dir / "sqlite").symlink_to(outside, target_is_directory=True)
    await _expect_init_failure(symlink_config)
    if list(outside.iterdir()):
        raise AssertionError("data init wrote through an escaping symlink")

    original_cwd = Path.cwd()
    os.chdir(work_dir)
    try:
        production = _config(work_dir / "safe-base").model_copy(
            update={"edition": "production", "data_dir": Path("relative-production-data")}
        )
        await _expect_init_failure(production)
        if (work_dir / "relative-production-data").exists():
            raise AssertionError("production relative path caused a write")
    finally:
        os.chdir(original_cwd)


def _probe_asset_integrity(work_dir: Path) -> None:
    demo_copy = work_dir / "tampered-demo"
    migrations_copy = work_dir / "tampered-migrations"
    shutil.copytree(Path(str(loader.resources.files("oria.resources.demo_data"))), demo_copy)
    shutil.copytree(Path(str(loader.resources.files("oria.migrations"))), migrations_copy)
    (demo_copy / "campaign_rules.v1.json").write_text("{}\n", encoding="utf-8")
    _expect_asset_failure(lambda: loader._verify_demo_tree(demo_copy))
    (migrations_copy / "platform" / "versions" / "platform_0001_catalog.py").unlink()
    _expect_asset_failure(lambda: loader._verify_migration_tree(migrations_copy))


async def _main(work_dir: Path) -> None:
    if work_dir.exists():
        raise AssertionError("bypass probe requires a fresh work directory")
    work_dir.mkdir(parents=True)
    await _probe_runtime_and_policy(work_dir)
    await _probe_migrations_and_paths(work_dir)
    _probe_asset_integrity(work_dir)
    print(
        "T03 bypass probe rejected storage exposure, hard-rule overrides, restricted-field "
        "serialization, migration detours, path escapes, and asset tampering"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.work_dir.resolve(strict=False)))


if __name__ == "__main__":
    main()
