"""Explicit API lifecycle uses real runtime, policy and SQLite approval storage."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.security.test_v06_api_identity import binding, identity_setup  # noqa: F401

from oria.api.server import build_api
from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.migrations.runner import MigrationError, upgrade_databases, validate_sqlite_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

# Imported pytest fixture parameter.
# ruff: noqa: F811
pytestmark = pytest.mark.integration


def settings(tmp_path):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("data_dir: data\n")
    identities = tmp_path / "identities.json"
    identities.write_text(json.dumps({"bindings": [binding().model_dump(mode="json")]}))
    server = tmp_path / "server.json"
    server.write_text(
        json.dumps(
            {
                "jwt": {
                    "issuer": "https://identity.example",
                    "audience": "oria",
                    "jwks_url": "https://identity.example/keys",
                },
                "identity_file": "identities.json",
                "approval_runtime_config": "runtime.yaml",
            }
        )
    )
    config = resolve_runtime_config(config_path=runtime, environ={}, cwd=tmp_path)
    return server, identities, config


def test_configured_approval_server_lifecycle(tmp_path, identity_setup, monkeypatch):
    auth, _, token, _ = identity_setup
    server, identities, config = settings(tmp_path)
    upgrade_databases(config)

    async def seed():
        now = datetime.now(UTC)
        async with DatabaseResources(config) as db:
            await SQLiteApprovalRepository(db.platform_sessions).add(
                Approval(
                    approval_id="lifecycle",
                    tenant_id="tenant",
                    approval_action="launch_approval",
                    tool_name="LaunchPlan",
                    canonical_args_hash="sha256:" + "a" * 64,
                    checkpoint_id="checkpoint",
                    policy_version="local-v2",
                    requester="requester",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )

    asyncio.run(seed())
    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", lambda **kw: auth.client)
    app = build_api(config=server, local=False, host="127.0.0.1")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token()
        detail = client.get("/v1/approvals/lifecycle")
        assert detail.status_code == 200, detail.text
        response = client.post(
            "/v1/approvals/lifecycle/decision",
            json={
                "decision": "approve",
                "expected_revision": detail.json()["revision"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "approved"
        identities.write_text('{"bindings": []}')
        assert client.get("/v1/approvals/lifecycle").status_code == 401
    assert auth.client.is_closed
    with sqlite3.connect(config.data_paths.platform_db) as db:
        assert db.execute(
            "SELECT status FROM approvals WHERE approval_id='lifecycle'"
        ).fetchone() == ("approved",)


def test_missing_database_stops_startup_without_creation(tmp_path, identity_setup, monkeypatch):
    auth, _, _, _ = identity_setup
    server, _, config = settings(tmp_path)
    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", lambda **kw: auth.client)
    app = build_api(config=server, local=False, host="127.0.0.1")
    with pytest.raises(MigrationError), TestClient(app):
        pytest.fail("startup unexpectedly succeeded")
    assert not config.data_paths.root.exists()
    assert auth.client.is_closed


def test_read_only_schema_gate_rejects_stale_head(tmp_path):
    _, _, config = settings(tmp_path)
    upgrade_databases(config)
    with sqlite3.connect(config.data_paths.platform_db) as db:
        db.execute("UPDATE alembic_version_platform SET version_num='platform_0009'")
    before = config.data_paths.platform_db.read_bytes()
    with pytest.raises(MigrationError):
        validate_sqlite_databases(config)
    assert config.data_paths.platform_db.read_bytes() == before


def test_startup_failure_releases_http_client(tmp_path, identity_setup, monkeypatch):
    auth, _, _, _ = identity_setup
    server, _, config = settings(tmp_path)
    upgrade_databases(config)
    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", lambda **kw: auth.client)

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr("oria.api.server.build_runtime", fail)
    with (
        pytest.raises(RuntimeError, match="synthetic startup failure"),
        TestClient(build_api(config=server, local=False, host="127.0.0.1")),
    ):
        pytest.fail("startup unexpectedly succeeded")
    assert auth.client.is_closed


def test_approval_config_does_not_consume_ambient_environment(tmp_path, monkeypatch):
    _, _, config = settings(tmp_path)
    monkeypatch.setenv("ORIA_LLM_PROFILE", "nonexistent-secret-provider")
    monkeypatch.setenv("ORIA_DATA_DIR", "/must-not-be-created")
    from oria.api.identity import IdentityDirectory
    from oria.api.server import approval_runtime

    upgrade_databases(config)

    async def run():
        async with approval_runtime(tmp_path / "runtime.yaml", lambda: IdentityDirectory()):
            pass

    asyncio.run(run())
