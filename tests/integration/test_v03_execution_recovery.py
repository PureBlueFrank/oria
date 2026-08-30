"""Recovery coverage for unknown outcomes, reconciliation, and database isolation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.core.execution_ledger import ExecutionLedger
from oria.core.types import EventEnvelope, ResourceRef
from oria.data import initialize_data
from oria.domain.ledger import DomainEvent, OutboxRecord, Receipt, ToolExecution
from oria.storage.database import DatabaseResources

pytestmark = [pytest.mark.integration, pytest.mark.recovery]

NOW = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
HASH_A = f"sha256:{'a' * 64}"
HASH_B = f"sha256:{'b' * 64}"
TENANT = "local-community"


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


def _reservation(execution_id: str) -> ToolExecution:
    return ToolExecution(
        execution_id=execution_id,
        tenant_id=TENANT,
        tool_name="publish_consumer_placement",
        idempotency_key=f"campaign_1:placement:{HASH_A}",
        canonical_args_hash=HASH_A,
        checkpoint_id="checkpoint_placement",
        created_at=NOW,
        updated_at=NOW,
    )


def _unknown_audit() -> EventEnvelope:
    return EventEnvelope(
        event_id="audit_unknown_1",
        occurred_at=NOW,
        tenant_id=TENANT,
        actor="operator_1",
        action="publish_consumer_placement",
        resource=ResourceRef(
            resource_type="campaign",
            resource_id="campaign_1",
            tenant_id=TENANT,
        ),
        decision="allow",
        policy_version="policy_v1",
        args_hash=HASH_A,
        result="failure",
        correlation_id="correlation_1",
        payload={"outcome": "unknown", "receipt_hash": HASH_B},
    )


@pytest.mark.asyncio
async def test_unknown_is_not_reinvoked_and_reconciliation_converges_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    calls = 0

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())

        async def invoke(_: str) -> Receipt:
            nonlocal calls
            calls += 1
            return Receipt(
                receipt_id="receipt_unknown_1",
                adapter_id="mock_placement",
                resource_ref="campaign:campaign_1",
                status="unknown",
                received_at=NOW + timedelta(seconds=1),
                summary_hash=HASH_B,
            )

        unknown = await ledger.execute(
            _reservation("exec_1"),
            invoke,
            audit_events=[_unknown_audit()],
        )
        duplicate = await ledger.execute(_reservation("exec_retry"), invoke)
        with pytest.raises(ValueError, match="required source state"):
            await ledger.mark_executing(unknown)

        reconciled_event = DomainEvent(
            event_id="event_reconciled_1",
            tenant_id=TENANT,
            aggregate_type="consumer_placement",
            aggregate_id="placement_1",
            event_type="consumer_placement.reconciled",
            event_version=1,
            payload={"receipt_hash": HASH_B},
            occurred_at=NOW + timedelta(seconds=3),
            correlation_id="correlation_1",
        )
        reconciled_outbox = OutboxRecord(
            event_id=reconciled_event.event_id,
            tenant_id=TENANT,
            topic=reconciled_event.event_type,
            payload_json=f'{{"receipt_hash":"{HASH_B}"}}',
            occurred_at=reconciled_event.occurred_at,
            available_at=reconciled_event.occurred_at,
        )
        reconciled = await ledger.reconcile(
            unknown,
            "succeeded",
            receipt_id="receipt_confirmed_1",
            domain_events=[reconciled_event],
            outbox_records=[reconciled_outbox],
        )

        async with databases.business_sessions() as session:
            execution_count = await session.scalar(text("SELECT COUNT(*) FROM tool_executions"))
            business_audit_count = await session.scalar(text("SELECT COUNT(*) FROM audit_events"))

    with sqlite3.connect(config.data_paths.platform_db) as connection:
        platform_audit_count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()

    assert unknown.status == duplicate.status == "unknown"
    assert unknown.compensation_status == "reconciliation_required"
    assert reconciled.status == "succeeded"
    assert reconciled.attempt_count == 1
    assert calls == 1
    assert execution_count == 1
    assert business_audit_count == 1
    assert platform_audit_count == (0,)
