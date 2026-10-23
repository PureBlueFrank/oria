"""Lease authority must still hold at the SQL COMMIT entry point."""

import pytest
from sqlalchemy import event
from tests.integration.test_v06_job_leases import _events, _save
from tests.integration.test_v06_job_leases import leases as leases

from oria.api.identity import AuthenticationError
from oria.core.job_leases import lease_token
from oria.core.jobs import JobConflict

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.recovery]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["claim", "heartbeat", "checkpoint", "finish"])
@pytest.mark.parametrize("change", ["identity", "policy", "clock"])
async def test_lease_commit_rechecks_final_authority(leases, monkeypatch, operation, change):
    s = leases
    job = await s.add()
    worker = s.service()
    before = job if operation == "claim" else await worker.claim(job.job_id)
    token = None if operation == "claim" else lease_token(before)
    if operation == "checkpoint":
        await _save(s, token, "commit-candidate")
    history = await _events(s, job.job_id)
    append = s.repo._append_event

    async def arm_commit_change(session, value):
        await append(session, value)

        def change_before_guard(_):
            if change == "identity":
                s.path.write_text('{"bindings": []}')
            elif change == "policy":
                monkeypatch.setattr(s.runtime.policy, "_trusted_actors", frozenset())
            else:
                s.clock.advance(30)

        event.listen(session.sync_session, "before_commit", change_before_guard, insert=True)

    monkeypatch.setattr(s.repo, "_append_event", arm_commit_change)
    with pytest.raises((AuthenticationError, PermissionError, JobConflict)):
        if operation == "claim":
            await worker.claim(job.job_id)
        elif operation == "heartbeat":
            await worker.heartbeat(token)
        elif operation == "checkpoint":
            await worker.publish_checkpoint(token, "commit-candidate")
        else:
            await worker.finish(token, "succeeded")
    assert await s.repo.get(job.tenant_id, job.job_id) == before
    assert await _events(s, job.job_id) == history
