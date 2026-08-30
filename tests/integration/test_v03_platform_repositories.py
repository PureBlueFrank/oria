"""V0.3-T02 SQLite approval and sanitized inbox repository integration tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.core.integration_events import ExternalWait, IntegrationEventInboxService
from oria.data import initialize_data
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository, SQLiteIntegrationEventInboxRepository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sqlite_approval_repository_round_trips_tenant_bound_state(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    approval = Approval(
        approval_id="approval-1",
        tenant_id="tenant-a",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash="sha256:" + "a" * 64,
        checkpoint_id="checkpoint-1",
        policy_version="policy-v1",
        expires_at=NOW + timedelta(hours=1),
        requester="requester-a",
        created_at=NOW,
        updated_at=NOW,
    )

    async with DatabaseResources(config) as databases:
        repository = SQLiteApprovalRepository(databases.platform_sessions)
        await repository.add(approval)

        assert await repository.get("tenant-a", "approval-1") == approval
        assert await repository.get("tenant-b", "approval-1") is None


@pytest.mark.asyncio
async def test_sqlite_inbox_enforces_dedup_and_persists_only_redacted_payload(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    wait = ExternalWait(
        tenant_id="tenant-a",
        wait_id="wait-1",
        event_type="merchant.enrollment_upserted",
        resource_type="campaign",
        resource_id="campaign-1",
        expected_version=1,
        checkpoint_id="checkpoint-1",
        expires_at=NOW + timedelta(hours=1),
        timeout_action="fail",
        created_at=NOW,
    )
    event = {
        "schema_version": 1,
        "event_type": "merchant.enrollment_upserted",
        "tenant_id": "tenant-a",
        "adapter_id": "adapter-a",
        "source_event_id": "source-event-1",
        "signature_subject": "adapter-principal-a",
        "version": 1,
        "payload": {
            "campaign_id": "campaign-1",
            "enrollment_id": "enrollment-1",
            "merchant_id": "synthetic-sensitive-merchant",
            "product_ref": "product-1",
            "product_version": "v1",
        },
    }

    async with DatabaseResources(config) as databases:
        repository = SQLiteIntegrationEventInboxRepository(databases.platform_sessions)
        await repository.add_wait(wait)
        service = IntegrationEventInboxService(
            repository,
            authorized_subjects={("tenant-a", "adapter-a"): frozenset({"adapter-principal-a"})},
            clock=lambda: NOW + timedelta(minutes=1),
        )
        assert (await service.process(event, wait=wait)).resume_eligible is True
        assert (await service.process(event, wait=wait)).status == "duplicate"

    with sqlite3.connect(config.data_paths.platform_db) as connection:
        row = connection.execute(
            "SELECT redacted_payload_json, payload_hash, processing_status "
            "FROM integration_event_inbox"
        ).fetchone()
    assert row is not None
    payload = json.loads(str(row[0]))
    assert payload["merchant_id"] == "[REDACTED]"
    assert "synthetic-sensitive-merchant" not in str(row)
    assert str(row[1]).startswith("sha256:")
    assert row[2] == "matched"
