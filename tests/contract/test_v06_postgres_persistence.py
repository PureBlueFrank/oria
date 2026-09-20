"""V0.6-T01 configuration, backend, migration, and saver contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError
from typer.testing import CliRunner

from oria.cli import app
from oria.config import ConfigResolutionError, resolve_runtime_config
from oria.migrations.runner import upgrade_databases
from oria.storage.database import (
    DatabaseResources,
    _clear_tenant_context,
    _create_postgres_engine,
    set_tenant_context,
)

pytestmark = pytest.mark.contract


class _Connection:
    def __init__(self, dialect_name: str = "postgresql") -> None:
        self.info: dict[str, str] = {}
        self.dialect = SimpleNamespace(name=dialect_name)
        self.driver_calls: list[tuple[str, Any]] = []

    def exec_driver_sql(self, statement: str, parameters: Any = None) -> None:
        self.driver_calls.append((statement, parameters))


def _write_config(tmp_path: Path, storage: str) -> Path:
    path = tmp_path / "runtime.yaml"
    path.write_text(storage, encoding="utf-8")
    return path


def test_postgres_backends_require_valid_explicit_urls_and_keep_secrets_out_of_repr(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        """\
storage:
  platform_db: postgres
  biz_db: postgres
  platform_url: ${ORIA_PLATFORM_DATABASE_URL}
  business_url: ${ORIA_BUSINESS_DATABASE_URL}
""",
    )
    platform_url = "postgresql://oria:platform-secret@db.example.test/platform"
    business_url = "postgresql+psycopg://oria:business-secret@db.example.test/business"

    resolved = resolve_runtime_config(
        config_path=config_path,
        environ={
            "ORIA_PLATFORM_DATABASE_URL": platform_url,
            "ORIA_BUSINESS_DATABASE_URL": business_url,
        },
        cwd=tmp_path,
    )

    assert resolved.storage.platform_db == "postgres"
    assert resolved.storage.biz_db == "postgres"
    assert resolved.storage.platform_url is not None
    assert resolved.storage.business_url is not None
    rendered = repr(resolved)
    summary = json.dumps(resolved.public_summary(), sort_keys=True)
    assert "platform-secret" not in rendered + summary
    assert "business-secret" not in rendered + summary
    assert resolved.public_summary()["storage"] == {
        "vector": "chroma",
        "platform_db": "postgres",
        "biz_db": "postgres",
        "cache": "memory",
        "object": "local",
        "platform_url_configured": True,
        "business_url_configured": True,
    }

    resources = DatabaseResources(resolved)
    try:
        assert resources.platform_backend == "postgres"
        assert resources.business_backend == "postgres"
    finally:
        # Engine creation is lazy; disposal must still be safe without a reachable server.
        asyncio.run(resources.aclose())


@pytest.mark.parametrize(
    ("storage", "message"),
    [
        (
            "storage:\n  platform_db: postgres\n  biz_db: sqlite\n",
            "platform PostgreSQL backend requires platform_url",
        ),
        (
            "storage:\n  platform_db: sqlite\n  biz_db: postgres\n  business_url: mysql://x/y\n",
            "business_url must use PostgreSQL",
        ),
        (
            "storage:\n  platform_db: sqlite\n  biz_db: sqlite\n  platform_url: postgresql://x/y\n",
            "platform_url is only valid",
        ),
    ],
)
def test_database_backend_configuration_fails_closed(
    tmp_path: Path, storage: str, message: str
) -> None:
    with pytest.raises(ConfigResolutionError, match=message):
        resolve_runtime_config(
            config_path=_write_config(tmp_path, storage),
            environ={},
            cwd=tmp_path,
        )


def test_two_postgres_revision_chains_require_distinct_databases(tmp_path: Path) -> None:
    shared = "postgresql://oria:secret@db.example.test/shared"
    with pytest.raises(ConfigResolutionError, match="distinct databases"):
        resolve_runtime_config(
            config_path=_write_config(
                tmp_path,
                """\
storage:
  platform_db: postgres
  biz_db: postgres
  platform_url: ${ORIA_PLATFORM_DATABASE_URL}
  business_url: ${ORIA_BUSINESS_DATABASE_URL}
""",
            ),
            environ={
                "ORIA_PLATFORM_DATABASE_URL": shared,
                "ORIA_BUSINESS_DATABASE_URL": shared,
            },
            cwd=tmp_path,
        )


def test_sql_bind_parameters_never_establish_tenant_identity() -> None:
    """A :tenant_id bind in arbitrary SQL must not set the RLS session identity."""

    engine = _create_postgres_engine("postgresql+psycopg://oria:secret@db.example.test/oria")
    try:
        assert not engine.sync_engine.dispatch.before_cursor_execute
    finally:
        asyncio.run(engine.dispose())


def test_explicit_trusted_api_establishes_tenant_identity() -> None:
    connection = _Connection()

    set_tenant_context(connection, "tenant-a")  # type: ignore[arg-type]

    assert connection.info == {"oria_tenant_id": "tenant-a"}
    assert connection.driver_calls == [
        ("SELECT set_config(%s, %s, true)", ("oria.tenant_id", "tenant-a"))
    ]

    # Re-asserting the same tenant inside one transaction is idempotent.
    set_tenant_context(connection, "tenant-a")  # type: ignore[arg-type]
    assert len(connection.driver_calls) == 1

    _clear_tenant_context(connection)  # type: ignore[arg-type]
    assert connection.info == {}


def test_tenant_switch_within_one_transaction_is_rejected() -> None:
    connection = _Connection()
    set_tenant_context(connection, "tenant-a")  # type: ignore[arg-type]

    with pytest.raises(InvalidRequestError, match="cannot switch tenant context"):
        set_tenant_context(connection, "tenant-b")  # type: ignore[arg-type]

    assert connection.info == {"oria_tenant_id": "tenant-a"}
    assert len(connection.driver_calls) == 1


def test_trusted_api_is_a_no_op_off_postgres() -> None:
    connection = _Connection(dialect_name="sqlite")

    set_tenant_context(connection, "tenant-a")  # type: ignore[arg-type]

    assert connection.info == {}
    assert connection.driver_calls == []


def test_db_upgrade_targets_are_programmatic_and_data_init_runner_compatible(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")

    platform = upgrade_databases(config, target="platform")
    assert platform.platform_revision == "platform_0009"
    assert platform.business_revision is None

    result = CliRunner().invoke(
        app,
        ["db", "upgrade", "--target", "business", "--data-dir", str(config.data_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "business_0011" in result.output

    both = upgrade_databases(config)
    assert both.platform_revision == "platform_0009"
    assert both.business_revision == "business_0011"
