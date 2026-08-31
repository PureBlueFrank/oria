"""Direct-path local SQLite integration tests for V0.1-T03."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from oria.cli import app
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator
from oria.storage.database import DatabaseResources

pytestmark = pytest.mark.integration


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _revision(path: Path, version_table: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(f'SELECT version_num FROM "{version_table}"').fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.asyncio
async def test_two_empty_databases_upgrade_seed_and_repeat_idempotently(tmp_path: Path) -> None:
    config = _config(tmp_path)

    first = await initialize_data(config)
    second = await initialize_data(config)

    assert first.merchants_inserted == 12
    assert second.merchants_inserted == 0
    assert first.platform_revision == second.platform_revision == "platform_0005"
    assert first.business_revision == second.business_revision == "business_0005"
    assert first.saver_setup is second.saver_setup is True
    platform_tables = _tables(config.data_paths.platform_db)
    business_tables = _tables(config.data_paths.business_db)
    assert {
        "documents",
        "document_versions",
        "ingestion_runs",
        "read_policy",
        "audit_events",
        "outbox",
    }.issubset(platform_tables)
    assert {"checkpoints", "writes"}.issubset(platform_tables)
    assert {
        "merchants",
        "campaigns",
        "coupon_batches",
        "enrollment_items",
        "tool_executions",
        "domain_events",
        "audit_events",
        "outbox",
    }.issubset(business_tables)
    assert "merchants" not in platform_tables
    assert not {"documents", "document_versions", "ingestion_runs"}.intersection(business_tables)
    assert _revision(config.data_paths.platform_db, "alembic_version_platform") == "platform_0005"
    assert _revision(config.data_paths.business_db, "alembic_version_business") == "business_0005"


@pytest.mark.asyncio
async def test_unique_runtime_assembly_exposes_service_not_repository(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="integration-session",
            thread_id="integration-thread",
            run_id="integration-run",
        )

        result = await ctx.domain.merchants.eligible_merchants("demo-east-dining-v1", 10, ctx)

        assert [merchant.merchant_id for merchant in result.merchants] == [
            "demo-m001",
            "demo-m002",
            "demo-m005",
            "demo-m006",
            "demo-m007",
            "demo-m008",
            "demo-m009",
            "demo-m010",
            "demo-m011",
            "demo-m012",
        ]
        assert result.evaluated_count == 12
    finally:
        await runtime.aclose()


def test_data_init_cli_reports_json_and_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "cli-data"
    runner = CliRunner()

    first = runner.invoke(app, ["data", "init", "--output", "json", "--data-dir", str(data_dir)])
    second = runner.invoke(app, ["data", "init", "--output", "json", "--data-dir", str(data_dir)])

    assert first.exit_code == second.exit_code == 0
    assert '"merchants_inserted": 12' in first.stdout
    assert '"merchants_inserted": 0' in second.stdout
    assert '"saver_setup": true' in second.stdout


@pytest.mark.asyncio
async def test_runtime_connections_enforce_platform_foreign_keys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)

    async with DatabaseResources(config) as databases:
        with pytest.raises(IntegrityError):
            async with databases.platform_sessions.begin() as session:
                enabled = await session.scalar(text("PRAGMA foreign_keys"))
                assert enabled == 1
                await session.execute(
                    text(
                        "INSERT INTO document_versions "
                        "(tenant_id, document_id, version, content_hash, object_ref, created_at) "
                        "VALUES (:tenant_id, :document_id, :version, :content_hash, "
                        ":object_ref, :created_at)"
                    ),
                    {
                        "tenant_id": "local-community",
                        "document_id": "missing-document",
                        "version": "v1",
                        "content_hash": "sha256:missing",
                        "object_ref": "object://missing",
                        "created_at": "2026-08-27T00:00:00+00:00",
                    },
                )


def test_data_init_cli_normalizes_migration_conflicts_to_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "conflicting-data"
    platform_db = data_dir / "sqlite" / "platform.db"
    platform_db.parent.mkdir(parents=True)
    with sqlite3.connect(platform_db) as connection:
        connection.execute("CREATE TABLE documents (wrong TEXT)")
        connection.commit()

    result = CliRunner().invoke(
        app,
        ["data", "init", "--output", "json", "--data-dir", str(data_dir)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "error": {
            "code": "data_init_failed",
            "message": "local data initialization failed closed",
        },
        "ok": False,
    }
    assert "Traceback" not in result.output
    assert "CREATE TABLE" not in result.output
