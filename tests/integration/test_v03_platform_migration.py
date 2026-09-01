"""V0.3-T02 platform_0004→0005, empty upgrade, and rollback tests."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

V03_T02_TABLES = {
    "approval_binding_invalidations",
    "approvals",
    "external_waits",
    "integration_event_inbox",
}


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


def _approval_values(action: str, status: str = "pending") -> tuple[object, ...]:
    timestamp = "2026-09-01T00:00:00+00:00"
    return (
        "tenant-a",
        f"approval-{action}",
        action,
        "tool-a",
        f"sha256:{'a' * 64}",
        "checkpoint-a",
        "policy-v1",
        "2026-09-02T00:00:00+00:00",
        status,
        "requester-a",
        None,
        None,
        None,
        timestamp,
        timestamp,
        None,
        "campaign-a",
        1,
        1,
        "selection-v1",
        f"sha256:{'b' * 64}",
    )


def test_empty_platform_database_upgrades_to_current_head_and_rolls_back_to_base(
    tmp_path: Path,
) -> None:
    database = tmp_path / "empty-platform.db"
    config = _config(database)

    command.upgrade(config, "head")

    assert _revision(database) == "platform_0007"
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
    assert _tables(database) >= V03_T02_TABLES - {"approval_binding_invalidations"}

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


def test_platform_0007_preserves_data_checks_and_indexes(tmp_path: Path) -> None:
    database = tmp_path / "platform-0007.db"
    config = _config(database)
    command.upgrade(config, "platform_0006")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            _approval_values("launch_approval"),
        )
        connection.commit()

    command.upgrade(config, "platform_0007")

    with sqlite3.connect(database) as connection:
        preserved = connection.execute(
            "SELECT approval_action, checkpoint_id, selection_version FROM approvals"
        ).fetchone()
        assert preserved == ("launch_approval", "checkpoint-a", "selection-v1")
        connection.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            _approval_values("assortment_submission_approval"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?)",
                _approval_values("unsupported_approval"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?)",
                _approval_values("merchant_notification_approval", "unsupported"),
            )
        indexes = {
            str(row[1]) for row in connection.execute('PRAGMA index_list("approvals")').fetchall()
        }
        assert {
            "ix_approvals_tenant_status_expires",
            "ix_approvals_tenant_campaign_status",
        } <= indexes


def test_platform_0007_invalidated_t06_approval_blocks_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "platform-0007-downgrade.db"
    config = _config(database)
    command.upgrade(config, "platform_0007")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            _approval_values("merchant_notification_approval", "invalidated"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="conditional T06 approvals"):
        command.downgrade(config, "platform_0006")

    assert _revision(database) == "platform_0007"
