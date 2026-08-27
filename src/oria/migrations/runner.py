"""Programmatic Alembic runner for the two installed-package revision chains."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import ValueModel
from oria.resources.loader import PackageAssetError, verify_migration_assets

_EXPECTED_TABLES = {
    "platform": frozenset({"documents", "document_versions", "ingestion_runs"}),
    "business": frozenset({"merchants"}),
}
_VERSION_TABLES = {
    "platform": "alembic_version_platform",
    "business": "alembic_version_business",
}


class MigrationError(RuntimeError):
    """Safe migration failure without SQL, filesystem internals, or resource contents."""


class MigrationResult(ValueModel):
    platform_revision: str
    business_revision: str


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _assert_paths(config: ResolvedRuntimeConfig) -> tuple[Path, Path]:
    if config.edition == "production" and not config.data_dir.is_absolute():
        raise MigrationError("production data initialization requires an absolute data directory")
    paths = config.data_paths
    root = paths.root.resolve(strict=False)
    database_paths = (paths.platform_db, paths.business_db)
    for database_path in database_paths:
        resolved = database_path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise MigrationError("database path escapes the configured data directory")
    return database_paths


def _upgrade_target(target: str, database_path: Path) -> None:
    script = resources.files(f"oria.migrations.{target}")
    if not script.is_dir():
        raise MigrationError("installed migration chain is unavailable")
    config = Config()
    config.set_main_option("script_location", str(script))
    config.set_main_option("sqlalchemy.url", _sqlite_url(database_path))
    try:
        command.upgrade(config, "head")
    except (CommandError, OSError, sqlite3.Error) as exc:
        raise MigrationError(f"{target} database migration failed") from exc


def _validate_target(target: str, database_path: Path, expected_head: str) -> None:
    try:
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            tables = {str(row[0]) for row in rows}
            version_table = _VERSION_TABLES[target]
            if version_table not in tables or not _EXPECTED_TABLES[target].issubset(tables):
                raise MigrationError(f"{target} database schema verification failed")
            revisions = connection.execute(f'SELECT version_num FROM "{version_table}"').fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"{target} database schema verification failed") from exc
    if revisions != [(expected_head,)]:
        raise MigrationError(f"{target} database revision verification failed")
    foreign_tables = _EXPECTED_TABLES["business" if target == "platform" else "platform"]
    if tables.intersection(foreign_tables):
        raise MigrationError(f"{target} database contains tables from the other revision chain")


def upgrade_databases(config: ResolvedRuntimeConfig) -> MigrationResult:
    """Upgrade platform then business using verified package resources only."""
    try:
        heads = verify_migration_assets()
    except PackageAssetError as exc:
        raise MigrationError("installed migration assets failed verification") from exc
    platform_db, business_db = _assert_paths(config)
    platform_db.parent.mkdir(parents=True, exist_ok=True)
    _upgrade_target("platform", platform_db)
    _validate_target("platform", platform_db, heads["platform"])
    _upgrade_target("business", business_db)
    _validate_target("business", business_db, heads["business"])
    return MigrationResult(
        platform_revision=heads["platform"],
        business_revision=heads["business"],
    )
