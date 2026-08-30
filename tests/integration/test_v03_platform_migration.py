"""V0.3-T02 platform_0004→0005, empty upgrade, and rollback tests."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

V03_T02_TABLES = {"approvals", "external_waits", "integration_event_inbox"}


def _config(database: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(resources.files("oria.migrations.platform")))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version_platform").fetchone()
    assert row is not None
    return str(row[0])


def _tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _pk_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5]))


def test_empty_platform_database_upgrades_to_0005_and_rolls_back_to_base(
    tmp_path: Path,
) -> None:
    database = tmp_path / "empty-platform.db"
    config = _config(database)

    command.upgrade(config, "head")

    assert _revision(database) == "platform_0005"
    assert _tables(database) >= V03_T02_TABLES

    command.downgrade(config, "base")

    assert V03_T02_TABLES.isdisjoint(_tables(database))
    assert "documents" not in _tables(database)


def test_platform_0004_upgrades_and_0005_rolls_back_without_touching_prior_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade-platform.db"
    config = _config(database)
    command.upgrade(config, "platform_0004")
    before = _tables(database)

    command.upgrade(config, "platform_0005")
    assert _revision(database) == "platform_0005"
    assert _tables(database) >= V03_T02_TABLES

    command.downgrade(config, "platform_0004")
    assert _revision(database) == "platform_0004"
    assert _tables(database) == before


def test_platform_0005_uses_tenant_scoped_keys_and_wait_foreign_key(tmp_path: Path) -> None:
    database = tmp_path / "constraints-platform.db"
    command.upgrade(_config(database), "head")

    with sqlite3.connect(database) as connection:
        assert _pk_columns(connection, "approvals") == ("tenant_id", "approval_id")
        assert _pk_columns(connection, "external_waits") == ("tenant_id", "wait_id")
        assert _pk_columns(connection, "integration_event_inbox") == (
            "tenant_id",
            "adapter_id",
            "source_event_id",
        )
        foreign_keys = connection.execute(
            'PRAGMA foreign_key_list("integration_event_inbox")'
        ).fetchall()
        assert {(str(row[3]), str(row[4])) for row in foreign_keys} == {
            ("tenant_id", "tenant_id"),
            ("wait_id", "wait_id"),
        }
