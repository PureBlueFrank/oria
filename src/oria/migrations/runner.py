"""Programmatic Alembic runner for the two installed-package revision chains."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import ValueModel
from oria.resources.loader import PackageAssetError, verify_migration_assets

ColumnSignature = tuple[str, str, int, int]
ForeignKeySignature = tuple[str, tuple[tuple[str, str], ...]]

_EXPECTED_COLUMNS: dict[str, dict[str, tuple[ColumnSignature, ...]]] = {
    "platform": {
        "documents": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("document_id", "VARCHAR", 1, 2),
            ("source_uri", "VARCHAR", 1, 0),
            ("owner_ref", "VARCHAR", 1, 0),
            ("data_classification", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("deleted_at", "DATETIME", 0, 0),
        ),
        "document_versions": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("document_id", "VARCHAR", 1, 2),
            ("version", "VARCHAR", 1, 3),
            ("content_hash", "VARCHAR", 1, 0),
            ("object_ref", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("acl_json", "TEXT", 1, 0),
            ("metadata_json", "TEXT", 1, 0),
            ("chunking_version", "VARCHAR", 1, 0),
            ("embedding_profile", "VARCHAR", 1, 0),
            ("deleted_at", "DATETIME", 0, 0),
            ("owner_ref", "VARCHAR", 1, 0),
            ("data_classification", "VARCHAR", 1, 0),
            ("superseded_at", "DATETIME", 0, 0),
        ),
        "ingestion_runs": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("run_id", "VARCHAR", 1, 2),
            ("document_id", "VARCHAR", 1, 0),
            ("document_version", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("started_at", "DATETIME", 1, 0),
            ("completed_at", "DATETIME", 0, 0),
        ),
        "rule_snapshot_cache": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("snapshot_id", "VARCHAR", 1, 2),
            ("snapshot_hash", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("created_at", "DATETIME", 1, 0),
        ),
        "read_policy": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("subject_id", "VARCHAR", 1, 2),
            ("allowed_roles_json", "TEXT", 1, 0),
            ("allowed_classifications_json", "TEXT", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
        "audit_events": (
            ("event_id", "VARCHAR", 1, 1),
            ("occurred_at", "DATETIME", 1, 0),
            ("tenant_id", "VARCHAR", 1, 0),
            ("actor", "VARCHAR", 1, 0),
            ("action", "VARCHAR", 1, 0),
            ("resource_type", "VARCHAR", 1, 0),
            ("resource_id", "VARCHAR", 1, 0),
            ("resource_tenant_id", "VARCHAR", 1, 0),
            ("decision", "VARCHAR", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("args_hash", "VARCHAR", 1, 0),
            ("result", "VARCHAR", 1, 0),
            ("correlation_id", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
        ),
        "outbox": (
            ("event_id", "VARCHAR", 1, 1),
            ("tenant_id", "VARCHAR", 1, 0),
            ("topic", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("occurred_at", "DATETIME", 1, 0),
            ("available_at", "DATETIME", 1, 0),
            ("published_at", "DATETIME", 0, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("last_error_code", "VARCHAR", 0, 0),
        ),
    },
    "business": {
        "merchants": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("merchant_id", "VARCHAR", 1, 2),
            ("version", "INTEGER", 1, 0),
            ("display_name", "VARCHAR", 1, 0),
            ("categories_json", "TEXT", 1, 0),
            ("cities_json", "TEXT", 1, 0),
            ("enrollment_systems_json", "TEXT", 1, 0),
            ("sales_org_code", "VARCHAR", 1, 0),
            ("active", "BOOLEAN", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
    },
}
_EXPECTED_FOREIGN_KEYS: dict[str, dict[str, frozenset[ForeignKeySignature]]] = {
    "platform": {
        "documents": frozenset(),
        "document_versions": frozenset(
            {
                (
                    "documents",
                    (("tenant_id", "tenant_id"), ("document_id", "document_id")),
                )
            }
        ),
        "ingestion_runs": frozenset(
            {
                (
                    "document_versions",
                    (
                        ("tenant_id", "tenant_id"),
                        ("document_id", "document_id"),
                        ("document_version", "version"),
                    ),
                )
            }
        ),
        "rule_snapshot_cache": frozenset(),
        "read_policy": frozenset(),
        "audit_events": frozenset(),
        "outbox": frozenset(),
    },
    "business": {"merchants": frozenset()},
}
_EXPECTED_TABLES = {target: frozenset(tables) for target, tables in _EXPECTED_COLUMNS.items()}
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
    except (CommandError, OSError, SQLAlchemyError, sqlite3.Error) as exc:
        raise MigrationError(f"{target} database migration failed") from exc


def _column_signature(connection: sqlite3.Connection, table: str) -> tuple[ColumnSignature, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)


def _foreign_key_signature(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[ForeignKeySignature]:
    rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    grouped: dict[int, list[tuple[int, str, str, str]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(
            (int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        )
    return frozenset(
        (
            sorted(parts)[0][1],
            tuple((source, destination) for _, _, source, destination in sorted(parts)),
        )
        for parts in grouped.values()
    )


def _validate_schema(connection: sqlite3.Connection, target: str) -> None:
    for table, expected_columns in _EXPECTED_COLUMNS[target].items():
        if _column_signature(connection, table) != expected_columns:
            raise MigrationError(f"{target} database schema verification failed")
        if _foreign_key_signature(connection, table) != _EXPECTED_FOREIGN_KEYS[target][table]:
            raise MigrationError(f"{target} database schema verification failed")


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
            _validate_schema(connection, target)
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
