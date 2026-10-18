"""Configured Feishu approval receiver over the application-owned runtime."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.contract.test_v06_feishu import event, request
from tests.integration.test_v06_api_lifecycle import settings
from tests.integration.test_v06_transactional_approval_acceptance import _approval, _document
from tests.security.test_v06_api_identity import binding, identity_setup  # noqa: F401

from oria.api.app import create_app
from oria.api.identity import file_directory
from oria.api.server import build_api
from oria.core.context import RuntimeServices
from oria.core.runtime import build_runtime
from oria.ingress.approval_cards import ApprovalCardBindings
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]
# Imported pytest fixture parameter.
# ruff: noqa: F811


def _settings(tmp_path, *, enabled=True):
    server, identities, config = settings(tmp_path)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    identities.write_text(json.dumps({"bindings": [identity.model_dump(mode="json")]}))
    document = json.loads(server.read_text())
    document["feishu"] = {
        "app_id": "app",
        "encrypt_key": "fixture-encrypt",
        "verification_token": "fixture-token",
    }
    if enabled:
        document["feishu_approval_receiver"] = True
    server.write_text(json.dumps(document))
    return server, identities, config, identity


async def _seed(config, identities, identity):
    upgrade_databases(config)
    async with DatabaseResources(config) as databases:
        runtime = await build_runtime(
            config,
            resource_factories=(lambda: databases,),
            trusted_actors=(identity.actor,),
            trusted_executors=(identity.executor,),
        )
        async with runtime:
            repository = SQLiteApprovalRepository(databases.platform_sessions)
            approval = await _approval(repository, datetime.now(UTC))
            return await ApprovalCardBindings(
                runtime, repository, file_directory(identities), databases.platform_sessions
            ).issue(approval.approval_id, identity, chat_id="chat", message_id="card-msg")


def _snapshot(config):
    with sqlite3.connect(config.data_paths.platform_db) as db:
        return {
            "status": db.execute("SELECT status FROM approvals").fetchone()[0],
            **{
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("ingress_receipts", "ingress_replays", "audit_events", "outbox")
            },
        }


def _post(client, document, *, nonce="server-action"):
    inbound = request(document, nonce=nonce)
    return _post_request(client, inbound)


def _post_request(client, inbound):
    return client.post(
        "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
    )


def _assert_approved_card(response):
    assert response.status_code == 200
    card = response.json()["card"]
    assert card["type"] == "raw" and card["data"]["config"]["update_multi"]
    assert "已批准" in response.text and "checkpoint-secret" not in response.text
    assert all(item["tag"] != "action" for item in card["data"]["elements"])


@contextmanager
def _running_server(server):
    # A pre-bound socket avoids port races. Each invocation boots a fresh process.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        script = (
            "import sys; from pathlib import Path; import uvicorn; "
            "from oria.api.server import build_api; "
            "uvicorn.run(build_api(config=Path(sys.argv[1]), local=False, host='127.0.0.1'), "
            "fd=int(sys.argv[2]), access_log=False, log_level='critical')"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(server), str(listener.fileno())],
            pass_fds=(listener.fileno(),),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        pytest.fail(f"server exited: {process.communicate()[0].decode()}")
                    try:
                        if client.get("/v1/health", timeout=0.2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        pytest.fail("server did not start in 30 seconds")
                    time.sleep(0.05)
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()


@pytest.mark.recovery
def test_configured_receiver_survives_process_restart_and_revocation(tmp_path):
    server, identities, config, identity = _settings(tmp_path)
    action = asyncio.run(_seed(config, identities, identity))
    baseline = _snapshot(config)
    with _running_server(server) as client:
        # Missing Job handler must not acknowledge a text message or leave a receipt.
        assert _post(client, event(), nonce="text").status_code == 503
        assert _snapshot(config) == baseline
        inbound = request(_document(action))
        _assert_approved_card(_post_request(client, inbound))
        committed = _snapshot(config)
        assert committed == {
            "status": "approved",
            "ingress_receipts": 1,
            "ingress_replays": 1,
            "audit_events": baseline["audit_events"] + 1,
            "outbox": baseline["outbox"] + 1,
        }
        _assert_approved_card(_post_request(client, inbound))
        assert _snapshot(config) == committed
    with _running_server(server) as client:
        _assert_approved_card(_post_request(client, inbound))
        assert _snapshot(config) == committed
        # A new signed delivery records its replay key, without a second decision.
        assert _post(client, _document(action), nonce="redelivery").status_code == 200
        committed["ingress_replays"] += 1
        assert _snapshot(config) == committed
        identities.write_text('{"bindings": []}')
        assert _post(client, _document(action)).status_code == 401
        assert _snapshot(config) == committed


def test_receiver_is_disabled_without_explicit_opt_in(tmp_path):
    server, identities, config, identity = _settings(tmp_path, enabled=False)
    action = asyncio.run(_seed(config, identities, identity))
    baseline = _snapshot(config)
    with TestClient(build_api(config=server, local=False, host="127.0.0.1")) as client:
        challenge = {"type": "url_verification", "token": "fixture-token", "challenge": "ok"}
        assert _post(client, challenge).json() == {"challenge": "ok"}
        assert _post(client, _document(action)).status_code == 503
        assert _snapshot(config) == baseline


def test_http_and_card_services_share_decisions_and_reject_stale_revision(
    tmp_path, identity_setup, monkeypatch
):
    auth, _, token, _ = identity_setup
    server, identities, config, identity = _settings(tmp_path)
    action = asyncio.run(_seed(config, identities, identity))
    identities.write_text(
        json.dumps({"bindings": [item.model_dump(mode="json") for item in (identity, binding())]})
    )
    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", lambda **kw: auth.client)
    with TestClient(build_api(config=server, local=False, host="127.0.0.1")) as client:
        client.headers["Authorization"] = "Bearer " + token()
        before = client.get("/v1/approvals/approval-card")
        assert before.status_code == 200
        assert before.json()["status"] == "pending"
        assert _post(client, _document(action)).status_code == 200
        after = client.get("/v1/approvals/approval-card")
        assert after.status_code == 200
        assert after.json()["status"] == "approved"
        assert after.json()["revision"] != before.json()["revision"]
        assert (
            client.post(
                "/v1/approvals/approval-card/decision",
                json={
                    "decision": "reject",
                    "reason": "synthetic rejection",
                    "expected_revision": before.json()["revision"],
                },
            ).status_code
            == 409
        )
        assert _snapshot(config)["ingress_receipts"] == 1
    assert auth.client.is_closed


@pytest.mark.parametrize("missing", ["feishu", "approval_runtime_config"])
def test_receiver_rejects_missing_prerequisite_without_creating_data(tmp_path, missing):
    server, _, config, _ = _settings(tmp_path)
    document = json.loads(server.read_text())
    del document[missing]
    server.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid server configuration"):
        build_api(config=server, local=False, host="127.0.0.1")
    assert not config.data_paths.root.exists()


def test_receiver_configuration_does_not_coerce_strings(tmp_path):
    server, _, config, _ = _settings(tmp_path)
    document = json.loads(server.read_text())
    document["feishu_approval_receiver"] = "true"
    server.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid server configuration"):
        build_api(config=server, local=False, host="127.0.0.1")
    assert not config.data_paths.root.exists()


def test_receiver_startup_failure_closes_runtime_database_and_http(tmp_path, monkeypatch):
    server, _, config, _ = _settings(tmp_path)
    upgrade_databases(config)
    closed = {"runtime": False, "database": False, "http": False}
    original_runtime_close = RuntimeServices.aclose
    original_close = DatabaseResources.aclose

    async def capture_runtime_close(runtime):
        await original_runtime_close(runtime)
        closed["runtime"] = True

    async def capture_database_close(databases):
        await original_close(databases)
        closed["database"] = True

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic receiver construction failure")

    http = httpx.AsyncClient()
    original_http_close = http.aclose

    async def capture_http_close():
        await original_http_close()
        closed["http"] = True

    monkeypatch.setattr(http, "aclose", capture_http_close)
    monkeypatch.setattr("oria.api.server.httpx.AsyncClient", lambda **kw: http)
    monkeypatch.setattr(RuntimeServices, "aclose", capture_runtime_close)
    monkeypatch.setattr("oria.api.server.TransactionalApprovalCardAcceptor", fail)
    monkeypatch.setattr(DatabaseResources, "aclose", capture_database_close)
    with (
        pytest.raises(RuntimeError, match="synthetic receiver construction failure"),
        TestClient(build_api(config=server, local=False, host="127.0.0.1")),
    ):
        pytest.fail("startup unexpectedly succeeded")
    assert closed == {"runtime": True, "database": True, "http": True}


def test_owned_services_cannot_be_mixed_with_injected_receiver():
    with pytest.raises(ValueError, match="cannot be mixed"):
        create_app(local_identity=binding(), services_factory=object(), receiver=object())
