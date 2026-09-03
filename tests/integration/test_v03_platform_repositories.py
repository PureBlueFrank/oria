"""V0.3-T02 SQLite approval and sanitized inbox repository integration tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.core.integration_events import (
    ExternalWait,
    IntegrationEventInboxService,
    IntegrationInboxIdentity,
    parse_integration_event,
)
from oria.core.types import EventEnvelope, ResourceRef
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
    audit_event = EventEnvelope(
        event_id="audit-approval-1",
        occurred_at=NOW,
        tenant_id="tenant-a",
        actor="requester-a",
        action="approval.created",
        resource=ResourceRef(
            resource_type="approval",
            resource_id="approval-1",
            tenant_id="tenant-a",
        ),
        decision="allow",
        policy_version="policy-v1",
        args_hash="sha256:" + "a" * 64,
        result="success",
        correlation_id="correlation-1",
        payload={"status": "pending", "api_key": "must-not-persist"},
    )

    async with DatabaseResources(config) as databases:
        repository = SQLiteApprovalRepository(databases.platform_sessions)
        await repository.add(approval, audit_event)

        assert await repository.get("tenant-a", "approval-1") == approval
        assert await repository.get("tenant-b", "approval-1") is None

        decided = Approval.model_validate(
            approval.model_dump()
            | {
                "status": "approved",
                "decider": "approver-a",
                "decision": "approve",
                "updated_at": NOW + timedelta(minutes=1),
                "decided_at": NOW + timedelta(minutes=1),
            }
        )
        with pytest.raises(ValueError, match="event already exists"):
            await repository.replace(decided, audit_event)
        assert await repository.get("tenant-a", "approval-1") == approval

    with sqlite3.connect(config.data_paths.platform_db) as connection:
        audit_payload = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_id = 'audit-approval-1'"
        ).fetchone()
        outbox_payload = connection.execute(
            "SELECT payload_json FROM outbox WHERE event_id = 'audit-approval-1'"
        ).fetchone()
    assert audit_payload is not None and outbox_payload is not None
    assert audit_payload == outbox_payload
    assert "must-not-persist" not in str(audit_payload)


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


@pytest.mark.asyncio
async def test_sqlite_inbox_atomically_validates_wait_and_consumes_selection_event(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    wait = ExternalWait(
        tenant_id="tenant-a",
        wait_id="selection-wait-1",
        event_type="selection.completed",
        resource_type="campaign",
        resource_id="campaign-1",
        expected_version=2,
        checkpoint_id="checkpoint-selection-1",
        expires_at=NOW + timedelta(hours=1),
        timeout_action="fail",
        created_at=NOW,
    )
    event_value = {
        "schema_version": 1,
        "event_type": "selection.completed",
        "tenant_id": "tenant-a",
        "adapter_id": "selection-adapter-a",
        "source_event_id": "selection-event-1",
        "signature_subject": "selection-principal-a",
        "version": 2,
        "payload": {
            "campaign_id": "campaign-1",
            "submission_version": "submission-1",
            "selection_version": "selection-1",
        },
    }
    event = parse_integration_event(event_value)
    identity = IntegrationInboxIdentity(
        tenant_id="tenant-a",
        adapter_id="selection-adapter-a",
        source_event_id="selection-event-1",
    )

    async with DatabaseResources(config) as databases:
        repository = SQLiteIntegrationEventInboxRepository(databases.platform_sessions)
        await repository.add_wait(wait)
        service = IntegrationEventInboxService(
            repository,
            authorized_subjects={
                ("tenant-a", "selection-adapter-a"): frozenset({"selection-principal-a"})
            },
            clock=lambda: NOW + timedelta(minutes=1),
        )
        assert (await service.process(event_value, wait=wait)).resume_eligible is True

        forged = event.model_copy(update={"signature_subject": "forged-principal"})
        with pytest.raises(PermissionError, match="does not match"):
            await repository.consume_matched(
                identity,
                forged,
                consumed_at=NOW + timedelta(minutes=2),
            )
        consumed = await repository.consume_matched(
            identity,
            event,
            consumed_at=NOW + timedelta(minutes=2),
        )
        assert consumed.record.processing_status == "consumed"
        assert consumed.wait.checkpoint_id == "checkpoint-selection-1"
        assert consumed.wait.status == "matched"
        with pytest.raises(PermissionError, match="does not match"):
            await repository.consume_matched(
                identity,
                event,
                consumed_at=NOW + timedelta(minutes=3),
            )

        async with databases.platform_sessions() as session:
            inbox_status = await session.scalar(
                text(
                    "SELECT processing_status FROM integration_event_inbox WHERE source_event_id "
                    "= 'selection-event-1'"
                )
            )
            wait_row = (
                await session.execute(
                    text(
                        "SELECT status, checkpoint_id FROM external_waits WHERE wait_id = "
                        "'selection-wait-1'"
                    )
                )
            ).one()
        assert inbox_status == "consumed"
        assert wait_row == ("matched", "checkpoint-selection-1")


@pytest.mark.asyncio
async def test_sqlite_inbox_rechecks_wait_expiry_when_claiming_a_matched_event(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    wait = ExternalWait(
        tenant_id="tenant-a",
        wait_id="selection-wait-expiring",
        event_type="selection.completed",
        resource_type="campaign",
        resource_id="campaign-1",
        expected_version=2,
        checkpoint_id="checkpoint-selection-expiring",
        expires_at=NOW + timedelta(minutes=2),
        timeout_action="fail",
        created_at=NOW,
    )
    event_value = {
        "schema_version": 1,
        "event_type": "selection.completed",
        "tenant_id": "tenant-a",
        "adapter_id": "selection-adapter-a",
        "source_event_id": "selection-event-expiring",
        "signature_subject": "selection-principal-a",
        "version": 2,
        "payload": {
            "campaign_id": "campaign-1",
            "submission_version": "submission-1",
            "selection_version": "selection-1",
        },
    }
    event = parse_integration_event(event_value)
    identity = IntegrationInboxIdentity(
        tenant_id="tenant-a",
        adapter_id="selection-adapter-a",
        source_event_id="selection-event-expiring",
    )

    async with DatabaseResources(config) as databases:
        repository = SQLiteIntegrationEventInboxRepository(databases.platform_sessions)
        await repository.add_wait(wait)
        service = IntegrationEventInboxService(
            repository,
            authorized_subjects={
                ("tenant-a", "selection-adapter-a"): frozenset({"selection-principal-a"})
            },
            clock=lambda: NOW + timedelta(minutes=1),
        )
        assert (await service.process(event_value, wait=wait)).resume_eligible is True

        with pytest.raises(PermissionError, match="does not match"):
            await repository.consume_matched(
                identity,
                event,
                consumed_at=wait.expires_at,
            )

        persisted_wait = await repository.get_wait("tenant-a", wait.wait_id)
        assert persisted_wait is not None
        assert persisted_wait.status == "waiting"
