"""Contract tests for the V0.2 platform ACL, audit, and outbox revision."""

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


def _tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version_platform").fetchone()
    assert row is not None
    return str(row[0])


def test_empty_platform_database_upgrades_to_v0003_and_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    config = _config(database)

    command.upgrade(config, "platform_0003")

    assert _revision(database) == "platform_0003"
    assert {"read_policy", "audit_events", "outbox"}.issubset(_tables(database))

    command.downgrade(config, "platform_0002")

    assert _revision(database) == "platform_0002"
    assert {"read_policy", "audit_events", "outbox"}.isdisjoint(_tables(database))
    assert {"documents", "document_versions", "rule_snapshot_cache"}.issubset(_tables(database))
