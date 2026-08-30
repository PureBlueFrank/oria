"""SQLite execution-ledger Repository integration tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.core.types import EventEnvelope, ResourceRef
from oria.data import initialize_data
from oria.domain.ledger import DomainEvent, OutboxRecord, ToolExecution
from oria.storage.database import DatabaseResources
from oria.storage.ledger import (
    SQLiteBusinessAuditRepository,
    SQLiteDomainEventRepository,
    SQLiteOutboxRepository,
    SQLiteToolExecutionRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
HASH_A = f"sha256:{'a' * 64}"
TENANT = "local-community"


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


def _execution(execution_id: str = "exec_1") -> ToolExecution:
    return ToolExecution(
        execution_id=execution_id,
        tenant_id=TENANT,
        tool_name="materialize_coupon_batch",
        idempotency_key=f"campaign_1:{HASH_A}",
        canonical_args_hash=HASH_A,
        checkpoint_id="checkpoint_1",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_tool_execution_repository_returns_duplicate_history_and_reconciles(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)

    async with DatabaseResources(config) as databases:
        repository = SQLiteToolExecutionRepository(databases.business_sessions)
        reserved = await repository.reserve(_execution())
        duplicate = await repository.reserve(_execution("exec_2"))
        executing = await repository.mark_executing(
            TENANT, reserved.execution_id, NOW + timedelta(seconds=1)
        )
        unknown = await repository.record_unknown(
            TENANT,
            executing.execution_id,
            NOW + timedelta(seconds=2),
        )
        reconciled = await repository.reconcile(
            TENANT,
            unknown.execution_id,
            "succeeded",
            NOW + timedelta(seconds=3),
            receipt_id="receipt_1",
        )
        history = await repository.get_by_idempotency(
            TENANT,
            reserved.tool_name,
            reserved.idempotency_key,
        )

    assert duplicate.execution_id == reserved.execution_id
    assert reconciled.status == "succeeded"
    assert reconciled.attempt_count == 1
    assert history == reconciled


@pytest.mark.asyncio
async def test_domain_audit_and_outbox_repositories_append_redacted_records(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    domain_event = DomainEvent(
        event_id="event_1",
        tenant_id=TENANT,
        aggregate_type="coupon_batch",
        aggregate_id="coupon_1",
        event_type="coupon_batch.materialized",
        event_version=1,
        payload={"api_key": "must-not-persist", "summary_hash": HASH_A},
        occurred_at=NOW,
        correlation_id="correlation_1",
    )
    audit_event = EventEnvelope(
        event_id="audit_1",
        occurred_at=NOW,
        tenant_id=TENANT,
        actor="operator_1",
        action="materialize_coupon_batch",
        resource=ResourceRef(
            resource_type="coupon_batch",
            resource_id="coupon_1",
            tenant_id=TENANT,
        ),
        decision="allow",
        policy_version="policy_v1",
        args_hash=HASH_A,
        result="success",
        correlation_id="correlation_1",
        payload={"prompt": "must-not-persist", "summary_hash": HASH_A},
    )
    outbox_record = OutboxRecord(
        event_id="event_1",
        tenant_id=TENANT,
        topic="coupon_batch.materialized",
        payload_json=f'{{"summary_hash":"{HASH_A}"}}',
        occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
        available_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    async with DatabaseResources(config) as databases:
        await SQLiteDomainEventRepository(databases.business_sessions).append(domain_event)
        await SQLiteBusinessAuditRepository(databases.business_sessions).append(audit_event)
        outbox = SQLiteOutboxRepository(databases.business_sessions)
        await outbox.append(outbox_record)
        assert await outbox.list_pending(TENANT, 10) == (outbox_record,)
        await outbox.mark_published(outbox_record.event_id)
        assert await outbox.list_pending(TENANT, 10) == ()

        async with databases.business_sessions() as session:
            domain_payload = await session.scalar(
                text("SELECT payload_json FROM domain_events WHERE event_id = 'event_1'")
            )
            audit_payload = await session.scalar(
                text("SELECT payload_json FROM audit_events WHERE event_id = 'audit_1'")
            )
            outbox_attempts = await session.scalar(
                text("SELECT attempt_count FROM outbox WHERE event_id = 'event_1'")
            )

    assert json.loads(str(domain_payload))["api_key"] == "[REDACTED]"
    assert json.loads(str(audit_payload))["prompt"] == "[REDACTED]"
    assert "must-not-persist" not in str(domain_payload) + str(audit_payload)
    assert outbox_attempts == 1
