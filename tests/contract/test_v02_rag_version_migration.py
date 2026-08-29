"""V0.2-T03 document-version lifecycle migration contracts."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.contract


def _config(database: Path) -> Config:
    script = resources.files("oria.migrations.platform")
    config = Config()
    config.set_main_option("script_location", str(script))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version_platform").fetchone()
    assert row is not None
    return str(row[0])


def _columns(database: Path, table: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_empty_database_upgrades_to_v0004_and_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    config = _config(database)

    command.upgrade(config, "platform_0004")

    assert _revision(database) == "platform_0004"
    assert {"owner_ref", "data_classification", "superseded_at"}.issubset(
        _columns(database, "document_versions")
    )

    command.downgrade(config, "platform_0003")

    assert _revision(database) == "platform_0003"
    assert {"owner_ref", "data_classification", "superseded_at"}.isdisjoint(
        _columns(database, "document_versions")
    )


def test_upgrade_backfills_version_policy_and_supersedes_older_completed_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-platform.db"
    config = _config(database)
    command.upgrade(config, "platform_0003")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO documents (tenant_id, document_id, source_uri, owner_ref, "
            "data_classification, created_at, deleted_at) VALUES "
            "('tenant-a', 'doc-a', 'fixture://doc-a', 'owner-a', 'restricted', "
            "'2026-08-28 00:00:00', NULL)"
        )
        for version, created_at in (
            ("v1", "2026-08-28 00:00:00"),
            ("v2", "2026-08-29 00:00:00"),
        ):
            connection.execute(
                "INSERT INTO document_versions (tenant_id, document_id, version, "
                "content_hash, object_ref, created_at, acl_json, metadata_json, "
                "chunking_version, embedding_profile, deleted_at) VALUES "
                "(?, 'doc-a', ?, ?, ?, ?, '{}', '{}', 'json-v1', 'fixture', NULL)",
                (
                    "tenant-a",
                    version,
                    f"sha256:{version}",
                    f"object://tenant-a/doc-a/{version}",
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO ingestion_runs (tenant_id, run_id, document_id, "
                "document_version, status, started_at, completed_at) VALUES "
                "('tenant-a', ?, 'doc-a', ?, 'completed', ?, ?)",
                (f"run-{version}", version, created_at, created_at),
            )
        connection.commit()

    command.upgrade(config, "platform_0004")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT version, owner_ref, data_classification, superseded_at "
            "FROM document_versions ORDER BY version"
        ).fetchall()
    assert rows[0][:3] == ("v1", "owner-a", "restricted")
    assert rows[0][3] is not None
    assert rows[1] == ("v2", "owner-a", "restricted", None)
