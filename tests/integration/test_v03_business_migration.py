"""V0.3-T01 empty/upgrade/rollback and tenant-constraint migration tests."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

V03_TABLES = {
    "product_snapshots",
    "campaign_rule_snapshot_refs",
    "campaigns",
    "coupon_batches",
    "launch_saga_states",
    "recruitment_publications",
    "enrollments",
    "enrollment_items",
    "enrollment_coupon_links",
    "confirmation_tasks",
    "assortment_submissions",
    "selection_decisions",
    "consumer_placements",
    "merchant_notifications",
}
EXPECTED_UNIQUE_KEYS = {
    "product_snapshots": ("tenant_id", "product_ref", "product_version"),
    "coupon_batches": ("tenant_id", "campaign_id", "coupon_spec_hash"),
    "recruitment_publications": (
        "tenant_id",
        "campaign_id",
        "merchant_scope_hash",
        "material_version",
    ),
    "enrollments": ("tenant_id", "campaign_id", "merchant_id"),
    "enrollment_items": (
        "tenant_id",
        "campaign_id",
        "merchant_id",
        "product_ref",
        "product_version",
    ),
    "enrollment_coupon_links": (
        "tenant_id",
        "enrollment_item_id",
        "coupon_batch_id",
        "benefit_tier",
    ),
    "consumer_placements": (
        "tenant_id",
        "campaign_id",
        "selection_version",
        "placement_spec_hash",
    ),
    "merchant_notifications": (
        "tenant_id",
        "merchant_id",
        "campaign_id",
        "result_version",
        "template_id",
        "channel",
    ),
}


def _config(database: Path) -> Config:
    script = resources.files("oria.migrations.business")
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
        row = connection.execute("SELECT version_num FROM alembic_version_business").fetchone()
    assert row is not None
    return str(row[0])


def _unique_indexes(connection: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    indexes: set[tuple[str, ...]] = set()
    for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if int(index[2]) != 1:
            continue
        columns = tuple(
            str(row[2]) for row in connection.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
        )
        indexes.add(columns)
    return indexes


def test_empty_business_database_upgrades_to_v03_and_rolls_back_to_base(
    tmp_path: Path,
) -> None:
    database = tmp_path / "empty-business.db"
    config = _config(database)

    command.upgrade(config, "head")

    assert _revision(database) == "business_0003"
    assert V03_TABLES | {"merchants"} <= _tables(database)

    command.downgrade(config, "base")

    assert V03_TABLES.isdisjoint(_tables(database))
    assert "merchants" not in _tables(database)


def test_v01_business_database_upgrades_to_v03_and_rolls_back_without_data_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v01-business.db"
    config = _config(database)
    command.upgrade(config, "business_0001")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO merchants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant-a",
                "merchant-1",
                1,
                "synthetic merchant",
                "[]",
                "[]",
                "[]",
                "sales-a",
                1,
                "2026-08-30T00:00:00+00:00",
                "2026-08-30T00:00:00+00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "business_0002")
    command.downgrade(config, "business_0001")

    assert _revision(database) == "business_0001"
    assert V03_TABLES.isdisjoint(_tables(database))
    with sqlite3.connect(database) as connection:
        merchant = connection.execute("SELECT tenant_id, merchant_id FROM merchants").fetchone()
    assert merchant == ("tenant-a", "merchant-1")


def test_every_business_foreign_key_is_tenant_composite_and_unique_keys_match_adr026(
    tmp_path: Path,
) -> None:
    database = tmp_path / "constraints.db"
    command.upgrade(_config(database), "head")

    with sqlite3.connect(database) as connection:
        for table in V03_TABLES:
            foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            grouped: dict[int, list[sqlite3.Row | tuple[object, ...]]] = {}
            for row in foreign_keys:
                grouped.setdefault(int(row[0]), []).append(row)
            for rows in grouped.values():
                source_to_target = {(str(row[3]), str(row[4])) for row in rows}
                assert ("tenant_id", "tenant_id") in source_to_target

        for table, expected_key in EXPECTED_UNIQUE_KEYS.items():
            assert expected_key in _unique_indexes(connection, table)
