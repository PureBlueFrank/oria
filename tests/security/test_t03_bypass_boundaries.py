"""Bypass-path assertions for T03 service, data, migration, and secrecy boundaries."""

from __future__ import annotations

import inspect
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

import oria.data as data_module
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import DataInitializationError, initialize_data
from oria.domain.models import EligibilityCriteria
from oria.permission.local import local_cli_executor, local_operator
from oria.resources import loader
from oria.resources.loader import PackageAssetError, load_demo_data

pytestmark = pytest.mark.security


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


@pytest.mark.asyncio
async def test_context_has_no_repository_engine_session_or_second_domain_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="bypass-session",
            thread_id="bypass-thread",
            run_id="bypass-run",
        )

        for name in ("repository", "repositories", "engine", "session", "session_factory"):
            assert not hasattr(ctx, name)
            assert not hasattr(runtime, name)
            assert not hasattr(ctx.domain, name)
        public_domain = {name for name in dir(ctx.domain) if not name.startswith("_")}
        assert public_domain == {"campaign_rules", "merchants"}
        parameters = inspect.signature(ctx.domain.merchants.eligible_merchants).parameters
        assert tuple(parameters) == ("rule_set_id", "limit", "ctx")
        with pytest.raises(TypeError):
            await ctx.domain.merchants.eligible_merchants(
                "demo-east-dining-v1",
                10,
                ctx,
                filters={"ignore_denylist": True},
            )
        with caplog.at_level(logging.DEBUG):
            result = await ctx.domain.merchants.eligible_merchants("demo-east-dining-v1", 100, ctx)
        candidate_ids = {merchant.merchant_id for merchant in result.merchants}
        assert "demo-m004" not in candidate_ids
        assert "demo-m011" not in candidate_ids
        assert "demo-m004" not in result.model_dump_json()
        assert "synthetic-east-a" not in result.model_dump_json()
        assert "demo-m004" not in caplog.text
        assert "synthetic-east-a" not in caplog.text
        caller_value = "caller-supplied-restricted-rule-value"
        with pytest.raises(LookupError) as excinfo:
            await ctx.domain.campaign_rules.get_rule_set(caller_value, ctx)
        assert caller_value not in str(excinfo.value)
    finally:
        await runtime.aclose()


def test_restricted_rules_and_sales_org_never_enter_public_projection_or_repr() -> None:
    bundle = load_demo_data()
    criteria = bundle.rules.internal_eligibility_criteria()
    rule_restricted_values = ("demo-m004", "synthetic-east-a", "synthetic-east-b")
    rule_projections = (
        repr(bundle.rules),
        bundle.rules.model_dump_json(),
        repr(criteria),
        criteria.model_dump_json(),
    )

    for projection in rule_projections:
        for restricted in rule_restricted_values:
            assert restricted not in projection
    merchant_projections = (repr(bundle.merchants), bundle.merchants.model_dump_json())
    for projection in merchant_projections:
        assert "synthetic-east-a" not in projection
        assert "synthetic-east-b" not in projection
    assert "allowlist_merchant_ids" not in criteria.model_dump()
    assert "denylist_merchant_ids" not in criteria.model_dump()
    assert "sales_org_scope" not in criteria.model_dump()


@pytest.mark.parametrize(
    "field",
    [
        "categories",
        "cities",
        "enrollment_systems",
        "allowlist_merchant_ids",
        "denylist_merchant_ids",
        "sales_org_scope",
    ],
)
def test_callers_cannot_use_empty_collection_semantics_to_relax_hard_rules(field: str) -> None:
    values = {
        "rule_set_id": "rules-v1",
        "rule_version": "1.0.0",
        "categories": ("餐饮",),
        "cities": ("上海",),
        "enrollment_systems": ("demo-enroll",),
        "allowlist_merchant_ids": ("merchant-a",),
        "denylist_merchant_ids": ("merchant-b",),
        "sales_org_scope": ("east-a",),
    }
    values[field] = ()

    with pytest.raises(ValidationError):
        EligibilityCriteria.model_validate(values)


def test_missing_or_modified_package_assets_fail_closed(tmp_path: Path) -> None:
    source_demo = Path(str(loader.resources.files("oria.resources.demo_data")))
    source_migrations = Path(str(loader.resources.files("oria.migrations")))
    demo_copy = tmp_path / "demo_data"
    migrations_copy = tmp_path / "migrations"
    shutil.copytree(source_demo, demo_copy)
    shutil.copytree(source_migrations, migrations_copy)
    assert loader._verify_demo_tree(demo_copy).version == "1.0.0"
    assert loader._verify_migration_tree(migrations_copy)["business"] == "business_0001"

    (demo_copy / "campaign_rules.v1.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PackageAssetError, match="integrity"):
        loader._verify_demo_tree(demo_copy)

    (migrations_copy / "business" / "versions" / "business_0001_merchants.py").unlink()
    with pytest.raises(PackageAssetError, match="unavailable"):
        loader._verify_migration_tree(migrations_copy)


@pytest.mark.asyncio
async def test_asset_failure_occurs_before_any_data_directory_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    def fail_assets() -> tuple[object, object]:
        raise PackageAssetError("tampered")

    monkeypatch.setattr(data_module, "verify_package_assets", fail_assets)
    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    assert not config.data_dir.exists()


@pytest.mark.asyncio
async def test_symlink_cannot_redirect_database_writes_outside_data_dir(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside-data-dir"
    outside.mkdir()
    config.data_dir.mkdir()
    (config.data_dir / "sqlite").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_production_relative_path_cannot_be_reintroduced_via_model_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path).model_copy(
        update={"edition": "production", "data_dir": Path("relative-production-data")}
    )

    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    assert not (tmp_path / "relative-production-data").exists()


@pytest.mark.asyncio
async def test_stamped_head_cannot_skip_schema_creation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.data_paths.platform_db.parent.mkdir(parents=True)
    with sqlite3.connect(config.data_paths.platform_db) as connection:
        connection.execute("CREATE TABLE alembic_version_platform (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version_platform VALUES ('platform_0002')")
        connection.commit()

    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    assert not config.data_paths.business_db.exists()


@pytest.mark.asyncio
async def test_stamped_head_with_lookalike_tables_cannot_skip_schema_creation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.data_paths.platform_db.parent.mkdir(parents=True)
    with sqlite3.connect(config.data_paths.platform_db) as connection:
        connection.execute("CREATE TABLE alembic_version_platform (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version_platform VALUES ('platform_0002')")
        connection.execute("CREATE TABLE documents (wrong TEXT)")
        connection.execute("CREATE TABLE document_versions (wrong TEXT)")
        connection.execute("CREATE TABLE ingestion_runs (wrong TEXT)")
        connection.commit()

    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    assert not config.data_paths.business_db.exists()


@pytest.mark.asyncio
async def test_revision_from_other_database_chain_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    with sqlite3.connect(config.data_paths.platform_db) as connection:
        connection.execute("UPDATE alembic_version_platform SET version_num = 'business_0001'")
        connection.commit()

    with pytest.raises(DataInitializationError, match="failed closed"):
        await initialize_data(config)
    with sqlite3.connect(config.data_paths.business_db) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version_business").fetchone()
    assert revision == ("business_0001",)
