"""Explicit job startup, process-resource recovery and non-destructive migration."""

from __future__ import annotations

# Imported pytest fixture parameters.
# ruff: noqa: F811
import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from tests.integration.test_v03_platform_migration import _config, _revision, _tables
from tests.integration.test_v06_api_lifecycle import settings
from tests.integration.test_v06_jobs import _body
from tests.integration.test_v06_transactional_approval_acceptance import _approval
from tests.security.test_v06_api_identity import identity_setup  # noqa: F401

from oria.api.server import ServerSettings, build_api
from oria.config import resolve_runtime_config
from oria.migrations.runner import upgrade_databases, validate_sqlite_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


def test_configured_job_runtime_recovers_after_restart_and_independent_process(
    tmp_path,
    identity_setup,
    monkeypatch,
):
    auth, state, token, _ = identity_setup
    server, _, config = settings(tmp_path)
    document = json.loads(server.read_text()) | {"job_service": True}
    server.write_text(json.dumps(document))
    upgrade_databases(config)
    client_type = httpx.AsyncClient

    def clients(**kwargs):
        return client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": state["keys"]})
            )
        )

    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", clients)
    with TestClient(build_api(config=server, local=False, host="127.0.0.1")) as client:
        client.headers["Authorization"] = "Bearer " + token()
        assert client.get("/v1/health").json()["job_service"] == "admission_only"
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]
        accepted = client.post(
            "/v1/jobs", json=_body(session_id), headers={"Idempotency-Key": "persist"}
        )
        assert accepted.status_code == 202, accepted.text
        view = accepted.json()
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/v1/jobs"]["post"]["parameters"][0]["name"] == "Idempotency-Key"
    # A separate interpreter reads the same typed job; no in-memory registry survives.
    script = """
import asyncio, sys
from pathlib import Path
from oria.config import resolve_runtime_config
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository
from oria.core.jobs import job_view
async def main():
    path = Path(sys.argv[1])
    config = resolve_runtime_config(config_path=path, environ={}, cwd=path.parent)
    async with DatabaseResources(config) as databases:
        job = await JobRepository(databases.platform_sessions).get('tenant', sys.argv[2])
        print(job_view(job).model_dump_json())
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "runtime.yaml"), view["job_id"]],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(child.stdout) == view
    with TestClient(build_api(config=server, local=False, host="127.0.0.1")) as client:
        client.headers["Authorization"] = "Bearer " + token()
        assert client.get(f"/v1/jobs/{view['job_id']}").json() == view
        assert (
            client.post(
                "/v1/jobs", json=_body(session_id), headers={"Idempotency-Key": "persist"}
            ).json()
            == view
        )
        assert client.post(f"/v1/jobs/{view['job_id']}/cancel").json()["status"] == "cancelled"
    asyncio.run(auth.client.aclose())


@pytest.mark.parametrize(
    "change", [{"job_service": True}, {"job_service": "true"}, {"job_service": 1}]
)
def test_jobs_require_an_explicit_runtime_and_strict_flag(change):
    with pytest.raises(ValueError):
        ServerSettings.model_validate(
            {
                "jwt": {
                    "issuer": "https://identity.example",
                    "audience": "oria",
                    "jwks_url": "https://identity.example/keys",
                },
                "identity_file": "identities.json",
                **change,
            }
        )


def test_job_service_is_off_by_default_and_local_mode_does_not_initialize_storage():
    with TestClient(
        build_api(config=None, local=True, host="127.0.0.1"), client=("127.0.0.1", 12345)
    ) as client:
        assert client.get("/v1/health").json()["job_service"] == "not_configured"
        assert client.post("/v1/sessions", json={}).status_code == 503
        assert client.get("/v1/jobs/unknown").status_code == 503


def test_job_migration_keeps_existing_approvals_and_refuses_data_loss(tmp_path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    path = config.data_paths.platform_db
    cfg = _config(path)
    command.downgrade(cfg, "platform_0012")
    before = _tables(path)

    async def seed():
        async with DatabaseResources(config) as databases:
            return await _approval(
                SQLiteApprovalRepository(databases.platform_sessions), datetime.now(UTC)
            )

    original = asyncio.run(seed())
    command.upgrade(cfg, "head")
    assert _revision(path) == "platform_0013"
    assert _tables(path) - before == {"sessions", "jobs", "job_events"}
    validate_sqlite_databases(config)

    async def read():
        async with DatabaseResources(config) as databases:
            return await SQLiteApprovalRepository(databases.platform_sessions).get(
                "tenant", original.approval_id
            )

    assert asyncio.run(read()) == original
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES ('tenant', :id, 'actor', 'issuer', 'subject', :created)",
            {"id": "session_" + "a" * 32, "created": datetime.now(UTC).isoformat()},
        )
    with pytest.raises(ValueError, match="cannot downgrade"):
        command.downgrade(cfg, "platform_0012")
    assert _revision(path) == "platform_0013"
    assert asyncio.run(read()) == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (1,)
