"""Durable job HTTP admission over real JWT, Policy and isolated SQLite databases."""

from __future__ import annotations

# Imported pytest fixtures are referenced by parameter names.
# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from tests.security.test_v06_api_identity import binding, identity_setup  # noqa: F401

from oria.api.app import create_app
from oria.api.identity import IdentityDirectory
from oria.api.jobs import JobAPI
from oria.config import resolve_runtime_config
from oria.core.jobs import Job, JobConflict
from oria.core.runtime import build_runtime
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources
from oria.storage.jobs import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest_asyncio.fixture
async def job_stack(tmp_path, identity_setup):
    auth, state, token, _ = identity_setup
    owner = binding()
    peer = binding(subject="peer").model_copy(
        update={"actor": owner.actor.model_copy(update={"subject_id": "peer-actor"})}
    )
    foreign = binding(subject="foreign").model_copy(
        update={
            "actor": owner.actor.model_copy(update={"tenant_id": "foreign"}),
            "executor": owner.executor.model_copy(update={"tenant_id": "foreign"}),
        }
    )
    admin = binding(subject="campaign-admin").model_copy(
        update={
            "actor": owner.actor.model_copy(
                update={"subject_id": "campaign-actor", "roles": ("campaign_admin",)}
            )
        }
    )
    state["directory"] = IdentityDirectory(bindings=(owner, peer, foreign, admin))

    def source():
        return state["directory"]

    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    async with DatabaseResources(config) as databases:
        runtime = await build_runtime(
            config,
            resource_factories=(lambda: databases,),
            trusted_actors=tuple(item.actor for item in source().bindings),
            trusted_executors=tuple(item.executor for item in source().bindings),
        )
        async with runtime:
            repository = JobRepository(databases.platform_sessions)
            api = JobAPI(runtime, repository, source)
            app = create_app(authenticator=auth, jobs=api)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client:
                client.headers["Authorization"] = "Bearer " + token()
                yield SimpleNamespace(
                    client=client,
                    api=api,
                    repository=repository,
                    databases=databases,
                    state=state,
                    token=token,
                    owner=owner,
                    source=source,
                    runtime=runtime,
                )
    await auth.client.aclose()


def _body(session_id):
    return {
        "session_id": session_id,
        "spec": {
            "scenario": "attribution",
            "metric": "redemptions",
            "period": {"start_date": "2026-07-01", "end_date": "2026-07-07"},
        },
    }


async def _session(client):
    response = await client.post("/v1/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _submit(client, session_id, key="idempotency-secret-sentinel", body=None):
    return await client.post(
        "/v1/jobs", json=body or _body(session_id), headers={"Idempotency-Key": key}
    )


async def _counts(stack):
    async with stack.databases.platform_sessions() as session:
        return {
            table: (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            for table in ("sessions", "jobs", "job_events")
        }


@pytest.mark.asyncio
@pytest.mark.recovery
async def test_job_admission_idempotency_restart_and_cancel(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    first = await _submit(stack.client, session_id)
    assert first.status_code == 202, first.text
    view = first.json()
    assert view["status"] == "queued" and view["attempt"] == 0
    assert (await stack.client.get("/v1/health")).json()["job_service"] == "admission_only"
    assert (await _submit(stack.client, session_id)).json() == view
    assert await _counts(stack) == {"sessions": 1, "jobs": 1, "job_events": 1}
    altered = _body(session_id)
    altered["spec"]["metric"] = "enrollments"
    assert (await _submit(stack.client, session_id, body=altered)).status_code == 409
    recreated = JobAPI(
        stack.runtime, JobRepository(stack.databases.platform_sessions), stack.source
    )
    assert (await recreated.detail(view["job_id"], stack.owner, "restart")).model_dump(
        mode="json"
    ) == view
    cancel = await stack.client.post(f"/v1/jobs/{view['job_id']}/cancel")
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
    assert (await stack.client.post(f"/v1/jobs/{view['job_id']}/cancel")).json() == cancel.json()
    assert await _counts(stack) == {"sessions": 1, "jobs": 1, "job_events": 2}
    assert (
        "requester" not in cancel.text
        and "checkpoint" not in cancel.text
        and "thread_id" not in cancel.text
    )
    async with stack.databases.platform_sessions() as session:
        persisted = str((await session.execute(text("SELECT * FROM jobs"))).all())
        assert stack.token() not in persisted and "idempotency-secret-sentinel" not in persisted
        assert "roles" not in persisted and "bearer" not in persisted
        assert (
            await session.execute(text("SELECT sequence, status FROM job_events ORDER BY sequence"))
        ).all() == [(0, "queued"), (1, "cancelled")]


@pytest.mark.asyncio
async def test_concurrent_same_key_has_one_job_and_initial_event(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    responses = await asyncio.gather(*(_submit(stack.client, session_id) for _ in range(5)))
    assert [response.status_code for response in responses] == [202] * 5
    assert len({response.json()["job_id"] for response in responses}) == 1
    assert await _counts(stack) == {"sessions": 1, "jobs": 1, "job_events": 1}
    job_id = responses[0].json()["job_id"]
    cancelled = await asyncio.gather(
        *(stack.client.post(f"/v1/jobs/{job_id}/cancel") for _ in range(3))
    )
    assert all(
        response.status_code == 200 and response.json()["status"] == "cancelled"
        for response in cancelled
    )
    assert await _counts(stack) == {"sessions": 1, "jobs": 1, "job_events": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["peer", "foreign"])
async def test_job_and_session_ownership_cannot_be_forged(job_stack, subject):
    stack = job_stack
    session_id = await _session(stack.client)
    job_id = (await _submit(stack.client, session_id)).json()["job_id"]
    stack.client.headers["Authorization"] = "Bearer " + stack.token(
        sub=subject, tenant_id="tenant", roles=["admin"]
    )
    for path in (f"/v1/jobs/{job_id}", "/v1/jobs/job_" + "0" * 32):
        response = await stack.client.get(path)
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert (await stack.client.post(f"/v1/jobs/{job_id}/cancel")).status_code == 404
    assert (await _submit(stack.client, session_id)).status_code == 404
    own_session = await _session(stack.client)
    own_job = await _submit(stack.client, own_session)
    assert own_job.status_code == 202 and own_job.json()["job_id"] != job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["tenant_id", "role", "actor", "executor", "checkpoint_id", "wait_id", "status", "lease_epoch"],
)
async def test_job_admission_rejects_client_authority_and_execution_state(job_stack, field):
    stack = job_stack
    session_id = await _session(stack.client)
    body = _body(session_id) | {field: "forged"}
    response = await _submit(stack.client, session_id, body=body)
    assert response.status_code == 422 and "forged" not in response.text
    assert (await stack.client.post("/v1/sessions", json={field: "forged"})).status_code == 422
    assert (await _counts(stack))["jobs"] == 0


@pytest.mark.asyncio
async def test_missing_key_unbounded_input_and_unauthenticated_requests_are_denied(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    assert (await stack.client.post("/v1/jobs", json=_body(session_id))).status_code == 422
    for key in ("", "bad key", "x" * 129):
        assert (await _submit(stack.client, session_id, key=key)).status_code == 422
    invalid = _body(session_id)
    invalid["spec"]["period"]["end_date"] = "2028-01-01"
    assert (await _submit(stack.client, session_id, body=invalid)).status_code == 422
    stack.client.headers.clear()
    assert (await _submit(stack.client, session_id)).status_code == 401
    assert (await stack.client.post("/v1/sessions", json={})).status_code == 401


@pytest.mark.asyncio
async def test_campaign_requires_existing_business_permission(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    body = {
        "session_id": session_id,
        "spec": {
            "scenario": "campaign",
            "categories": ["餐饮"],
            "cities": ["上海"],
            "enrollment_mode": "hybrid",
        },
    }
    assert (await _submit(stack.client, session_id, body=body)).status_code == 404
    assert (await _counts(stack))["jobs"] == 0
    stack.client.headers["Authorization"] = "Bearer " + stack.token(sub="campaign-admin")
    session_id = await _session(stack.client)
    body["session_id"] = session_id
    accepted = await _submit(stack.client, session_id, body=body)
    assert accepted.status_code == 202 and accepted.json()["scenario"] == "campaign"


@pytest.mark.asyncio
async def test_identity_revocation_during_job_write_rolls_back_everything(job_stack, monkeypatch):
    stack = job_stack
    session_id = await _session(stack.client)
    append = stack.repository._append_event

    async def revoke(session, job):
        await append(session, job)
        stack.state["directory"] = IdentityDirectory()

    monkeypatch.setattr(stack.repository, "_append_event", revoke)
    assert (await _submit(stack.client, session_id)).status_code == 401
    assert await _counts(stack) == {"sessions": 1, "jobs": 0, "job_events": 0}


@pytest.mark.asyncio
async def test_event_failure_rolls_back_job_and_does_not_poison_idempotency(job_stack, monkeypatch):
    stack = job_stack
    session_id = await _session(stack.client)
    original = stack.repository._append_event

    async def fail(session, job):
        raise RuntimeError("private-failure-sentinel")

    monkeypatch.setattr(stack.repository, "_append_event", fail)
    response = await _submit(stack.client, session_id)
    assert response.status_code == 500 and "private-failure-sentinel" not in response.text
    assert (await _counts(stack))["jobs"] == 0
    monkeypatch.setattr(stack.repository, "_append_event", original)
    assert (await _submit(stack.client, session_id)).status_code == 202
    assert (await _counts(stack))["job_events"] == 1


async def _trusted_guard():
    return None


@pytest.mark.asyncio
async def test_running_cancel_is_cooperative_and_stale_success_cannot_overwrite_it(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    job_id = (await _submit(stack.client, session_id)).json()["job_id"]
    queued = await stack.repository.get("tenant", job_id)
    now = datetime.now(UTC)
    running = Job.model_validate(
        queued.model_dump()
        | {
            "status": "running",
            "revision": 1,
            "attempt": 1,
            "lease_owner": "worker",
            "lease_epoch": 1,
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=30),
            "updated_at": now,
        }
    )
    await stack.repository.replace(queued, running, _trusted_guard)
    response = await stack.client.post(f"/v1/jobs/{job_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancellation_requested"
    stale = Job.model_validate(
        running.model_dump()
        | {
            "status": "succeeded",
            "revision": 2,
            "lease_owner": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
            "updated_at": datetime.now(UTC),
        }
    )
    with pytest.raises(JobConflict):
        await stack.repository.replace(running, stale, _trusted_guard)
    assert (await stack.repository.get("tenant", job_id)).status == "cancellation_requested"


@pytest.mark.asyncio
async def test_policy_change_during_write_rolls_back_then_audits_denial(job_stack, monkeypatch):
    stack = job_stack
    session_id = await _session(stack.client)
    original = stack.repository._append_event

    async def revoke_policy(session, job):
        await original(session, job)
        monkeypatch.setattr(stack.runtime.policy, "_trusted_actors", frozenset())

    monkeypatch.setattr(stack.repository, "_append_event", revoke_policy)
    response = await _submit(stack.client, session_id)
    assert response.status_code == 404
    assert await _counts(stack) == {"sessions": 1, "jobs": 0, "job_events": 0}
    async with stack.databases.platform_sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE decision = 'deny' "
                    "AND action = 'session:read'"
                )
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting_approval", "waiting_event", "retry_scheduled"])
async def test_waiting_and_retry_jobs_cancel_without_execution(job_stack, status):
    stack = job_stack
    session_id = await _session(stack.client)
    job_id = (await _submit(stack.client, session_id)).json()["job_id"]
    queued = await stack.repository.get("tenant", job_id)
    now = datetime.now(UTC)
    running = Job.model_validate(
        queued.model_dump()
        | {
            "status": "running",
            "revision": 1,
            "attempt": 1,
            "lease_owner": "worker",
            "lease_epoch": 1,
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=30),
            "updated_at": now,
        }
    )
    await stack.repository.replace(queued, running, _trusted_guard)
    waiting = Job.model_validate(
        running.model_dump()
        | {
            "status": status,
            "revision": 2,
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "accepted_checkpoint_ns": "epoch-1",
            "accepted_checkpoint_id": "private-checkpoint",
            "updated_at": datetime.now(UTC),
            "retry_at": now + timedelta(minutes=1) if status == "retry_scheduled" else None,
        }
    )
    await stack.repository.replace(running, waiting, _trusted_guard)
    response = await stack.client.post(f"/v1/jobs/{job_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert "private-checkpoint" not in response.text
    assert (await _counts(stack))["job_events"] == 4


@pytest.mark.asyncio
async def test_repository_rejects_terminal_revival_and_immutable_changes(job_stack):
    stack = job_stack
    session_id = await _session(stack.client)
    job_id = (await _submit(stack.client, session_id)).json()["job_id"]
    queued = await stack.repository.get("tenant", job_id)
    await stack.client.post(f"/v1/jobs/{job_id}/cancel")
    cancelled = await stack.repository.get("tenant", job_id)
    for change in ({"status": "queued"}, {"status": "failed", "thread_id": "other-thread"}):
        invalid = Job.model_validate(
            cancelled.model_dump()
            | change
            | {
                "revision": cancelled.revision + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        with pytest.raises(JobConflict):
            await stack.repository.replace(cancelled, invalid, _trusted_guard)
    illegal = Job.model_validate(queued.model_dump() | {"status": "succeeded", "revision": 1})
    with pytest.raises(JobConflict):
        await stack.repository.replace(queued, illegal, _trusted_guard)
    assert (await _counts(stack))["job_events"] == 2
