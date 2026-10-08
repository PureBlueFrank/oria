"""Approval HTTP boundaries against real signatures, policy and SQLite persistence."""

# Imported pytest fixtures are intentionally referenced by test parameter names.
# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from tests.security.test_v06_api_identity import identity_setup  # noqa: F401

from oria.api.app import create_app
from oria.api.approvals import ApprovalAPI
from oria.api.identity import IdentityDirectory
from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.migrations.runner import upgrade_databases
from oria.permission.local import LocalPolicyEngine
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = pytest.mark.security


@pytest.mark.asyncio
async def test_approval_http_boundaries(tmp_path, identity_setup):
    auth, state, token, _ = identity_setup
    identity = state["directory"].bindings[0]
    policy = LocalPolicyEngine(
        trusted_actors=(identity.actor,), trusted_executors=(identity.executor,)
    )
    runtime = SimpleNamespace(
        policy=policy,
        new_context=lambda **kw: SimpleNamespace(
            **kw,
            tenant_id=kw["actor"].tenant_id,
            correlation_id=kw["run_id"],
        ),
    )
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    now = datetime.now(UTC)
    async with DatabaseResources(config) as db:
        repository = SQLiteApprovalRepository(db.platform_sessions)
        pending = Approval(
            approval_id="safe",
            tenant_id=identity.actor.tenant_id,
            approval_action="launch_approval",
            tool_name="LaunchPlan",
            canonical_args_hash="sha256:" + "a" * 64,
            checkpoint_id="private-checkpoint",
            policy_version="local-v2",
            requester="private-requester",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=1),
        )
        for item in (
            pending,
            pending.model_copy(
                update={"approval_id": "own", "requester": identity.actor.subject_id}
            ),
            pending.model_copy(update={"approval_id": "foreign", "tenant_id": "another-tenant"}),
            pending.model_copy(
                update={
                    "approval_id": "expired",
                    "created_at": now - timedelta(hours=2),
                    "expires_at": now - timedelta(hours=1),
                }
            ),
        ):
            await repository.add(item)
        api = ApprovalAPI(runtime, repository, lambda: state["directory"])
        app = create_app(authenticator=auth, approvals=api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/approvals/safe")).status_code == 401
            client.headers["Authorization"] = "Bearer " + token(
                tenant_id="another-tenant", roles=["admin"]
            )
            detail = await client.get("/v1/approvals/safe")
            assert detail.status_code == 200
            assert "private-checkpoint" not in detail.text
            assert "private-requester" not in detail.text
            assert pending.canonical_args_hash not in detail.text
            assert detail.headers["cache-control"] == "no-store"
            revision = detail.json()["revision"]
            for ref in ("foreign", "missing"):
                response = await client.get(f"/v1/approvals/{ref}")
                assert response.status_code == 404
                assert response.json()["code"] == "not_found"
            assert (await client.get("/v1/approvals/safe?page=2")).status_code == 404
            assert (await client.get("/v1/approvals/safe?page=0")).status_code == 422
            body = {"decision": "approve", "expected_revision": revision}
            for extra in ("tenant_id", "role", "checkpoint_id", "wait_id"):
                assert (
                    await client.post("/v1/approvals/safe/decision", json=body | {extra: "forged"})
                ).status_code == 422
            assert (
                await client.post(
                    "/v1/approvals/safe/decision", json=body | {"decision": "reject", "reason": " "}
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/approvals/safe/decision", json=body | {"expected_revision": "0" * 64}
                )
            ).status_code == 409
            for ref in ("own", "expired"):
                current = (await client.get(f"/v1/approvals/{ref}")).json()["revision"]
                assert (
                    await client.post(
                        f"/v1/approvals/{ref}/decision", json=body | {"expected_revision": current}
                    )
                ).status_code == 404
            response = await client.post("/v1/approvals/safe/decision", json=body)
            assert response.status_code == 200
            assert response.json() == {"approval_id": "safe", "status": "approved"}
            assert (await client.post("/v1/approvals/safe/decision", json=body)).status_code == 409
            final = await repository.get(identity.actor.tenant_id, "safe")
            assert final.decider == identity.actor.subject_id
            async with db.platform_sessions() as session:
                assert (
                    await session.execute(
                        text("SELECT count(*) FROM audit_events WHERE action='approval.decided'")
                    )
                ).scalar_one() == 1
            # A valid signature with a currently mapped but unauthorized role is denied.
            restricted = identity.model_copy(
                update={"actor": identity.actor.model_copy(update={"roles": ("operator",)})}
            )
            state["directory"] = IdentityDirectory(bindings=(restricted,))
            assert (await client.get("/v1/approvals/safe")).status_code == 404
            assert (await client.post("/v1/approvals/safe/decision", json=body)).status_code == 404
            # Directory changes invalidate existing signed tokens immediately.
            state["directory"] = IdentityDirectory()
            assert (await client.get("/v1/approvals/safe")).status_code == 401
            assert (await client.post("/v1/approvals/safe/decision", json=body)).status_code == 401
        await auth.client.aclose()


@pytest.mark.asyncio
async def test_unconfigured_approval_service_fails_closed(identity_setup):
    auth, _, token, _ = identity_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(authenticator=auth)), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + token()
        assert (await client.get("/v1/approvals/anything")).status_code == 503
    await auth.client.aclose()
