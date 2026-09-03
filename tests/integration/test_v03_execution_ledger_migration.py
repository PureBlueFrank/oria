"""Business execution-ledger migration integration tests."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

LEDGER_TABLES = {"tool_executions", "domain_events", "audit_events", "outbox"}


def _config(database: Path, target: str = "business") -> Config:
    script = resources.files(f"oria.migrations.{target}")
    config = Config()
    config.set_main_option("script_location", str(script))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _revision(database: Path, target: str = "business") -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute(f"SELECT version_num FROM alembic_version_{target}").fetchone()
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


def test_business_0003_empty_upgrade_is_repeatable_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    database = tmp_path / "business.db"
    config = _config(database)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert _revision(database) == "business_0010"
    assert _tables(database) >= LEDGER_TABLES

    command.downgrade(config, "business_0002")

    assert _revision(database) == "business_0002"
    assert LEDGER_TABLES.isdisjoint(_tables(database))
    assert {"merchants", "campaigns"} <= _tables(database)


def test_execution_ledger_constraints_enforce_idempotency_status_and_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "constraints.db"
    command.upgrade(_config(database), "head")
    base_values = (
        "exec_1",
        "tenant_a",
        "publish_recruitment",
        "campaign_1:publish:hash",
        f"sha256:{'a' * 64}",
        "checkpoint_1",
        "reserved",
        None,
        None,
        0,
        "2026-08-30T00:00:00+00:00",
        "2026-08-30T00:00:00+00:00",
        None,
        None,
    )
    sql = (
        "INSERT INTO tool_executions (execution_id, tenant_id, tool_name, idempotency_key, "
        "canonical_args_hash, checkpoint_id, status, receipt_id, compensation_status, "
        "attempt_count, created_at, updated_at, executed_at, receipt_summary_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    with sqlite3.connect(database) as connection:
        connection.execute(sql, base_values)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, ("exec_2", *base_values[1:]))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, ("exec_3", *base_values[1:6], "invalid", *base_values[7:]))
        succeeded_without_receipt = (
            "exec_4",
            "tenant_a",
            "publish_recruitment",
            "campaign_2:publish:hash",
            *base_values[4:6],
            "succeeded",
            None,
            None,
            1,
            *base_values[10:12],
            "2026-08-30T00:00:01+00:00",
            None,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, succeeded_without_receipt)


def test_business_0010_preserves_existing_terminal_executions_and_downgrades(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipt-evidence.db"
    config = _config(database)
    command.upgrade(config, "business_0009")
    timestamp = "2026-09-03T00:00:00+00:00"
    insert = (
        "INSERT INTO tool_executions (execution_id, tenant_id, tool_name, idempotency_key, "
        "canonical_args_hash, checkpoint_id, status, receipt_id, compensation_status, "
        "attempt_count, created_at, updated_at, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)"
    )
    common = (
        "tenant_a",
        "publish_recruitment",
        f"sha256:{'a' * 64}",
        "checkpoint_1",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            insert,
            (
                "exec_failed",
                *common[:2],
                "campaign_1:failed",
                *common[2:],
                "failed",
                None,
                None,
                1,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            insert,
            (
                "exec_succeeded",
                *common[:2],
                "campaign_1:succeeded",
                *common[2:],
                "succeeded",
                "receipt_succeeded",
                None,
                1,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT execution_id, status, receipt_id, receipt_summary_hash FROM "
            "tool_executions ORDER BY execution_id"
        ).fetchall()
        connection.execute(
            "UPDATE tool_executions SET receipt_id = 'receipt_rejected', "
            "receipt_summary_hash = ? WHERE execution_id = 'exec_failed'",
            (f"sha256:{'b' * 64}",),
        )
        connection.commit()
    assert rows == [
        ("exec_failed", "failed", None, None),
        ("exec_succeeded", "succeeded", "receipt_succeeded", None),
    ]

    command.downgrade(config, "business_0009")

    with sqlite3.connect(database) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(tool_executions)")}
        failed_receipt = connection.execute(
            "SELECT receipt_id FROM tool_executions WHERE execution_id = 'exec_failed'"
        ).fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "receipt_summary_hash" not in columns
    assert failed_receipt == (None,)
    assert foreign_key_errors == []


def test_platform_and_business_revision_chains_upgrade_independently(tmp_path: Path) -> None:
    platform = tmp_path / "platform.db"
    business = tmp_path / "business.db"

    command.upgrade(_config(platform, "platform"), "head")
    command.upgrade(_config(business), "head")
    command.downgrade(_config(business), "business_0002")

    assert _revision(platform, "platform") == "platform_0007"
    assert _revision(business) == "business_0002"
    assert "tool_executions" not in _tables(platform)
    assert LEDGER_TABLES.isdisjoint(_tables(business))


def test_business_0004_enforces_and_downgrades_launch_saga_states(tmp_path: Path) -> None:
    database = tmp_path / "launch-saga.db"
    config = _config(database)
    command.upgrade(config, "head")
    timestamp = "2026-08-31T00:00:00+00:00"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO campaign_rule_snapshot_refs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant_a",
                "rule_ref_1",
                1,
                timestamp,
                timestamp,
                "rs_123456789012345678901234",
                f"sha256:{'a' * 64}",
            ),
        )
        connection.execute(
            "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant_a",
                "campaign_1",
                1,
                timestamp,
                timestamp,
                "rule_ref_1",
                "hybrid",
                "draft",
            ),
        )
        connection.execute(
            "INSERT INTO launch_saga_states VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant_a",
                "saga_1",
                1,
                timestamp,
                timestamp,
                "campaign_1",
                "planned",
                "checkpoint_1",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE launch_saga_states SET status = 'pending' WHERE launch_saga_id = 'saga_1'"
            )
        connection.commit()

    command.downgrade(config, "business_0003")

    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM launch_saga_states WHERE launch_saga_id = 'saga_1'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE launch_saga_states SET status = 'planned' WHERE launch_saga_id = 'saga_1'"
            )

    assert status == ("pending",)
