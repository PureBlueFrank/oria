"""Recovery coverage for unknown outcomes, reconciliation, and database isolation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import oria.domain.ledger as _domain_ledger  # noqa: F401  # Initialize domain package first.
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


@dataclass(frozen=True, slots=True)
class _MerchantReconciliationProjection:
    tenant_id: str
    execution_id: str
    aggregate_type: str
    aggregate_id: str
    outcome: Literal["failed", "unknown"]

    async def apply(self, session: AsyncSession) -> None:
        await session.execute(
            text(
                "UPDATE merchants SET version = version + 1 WHERE tenant_id = :tenant_id "
                "AND merchant_id = 'demo-m001'"
            ),
            {"tenant_id": self.tenant_id},
        )


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


@pytest.mark.asyncio
async def test_stale_executing_is_atomically_moved_to_reconciliation_without_reinvocation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    clock = _Clock()
    adapter_calls = 0

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(
            databases.business_sessions,
            clock=clock,
            executing_timeout=timedelta(minutes=5),
        )
        executing = await ledger.mark_executing(await ledger.reserve(_reservation("exec_stale")))
        clock.value += timedelta(minutes=6)

        async def invoke(_: str) -> Receipt:
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("stale recovery must not invoke the adapter")

        outbox = OutboxRecord(
            event_id="event_stale_unknown",
            tenant_id=TENANT,
            topic="consumer_placement.unknown",
            payload_json='{"outcome":"unknown"}',
            occurred_at=clock.value,
            available_at=clock.value,
        )
        unknown = await ledger.recover_stale_executing(
            executing,
            outcome_projection=_MerchantReconciliationProjection(
                tenant_id=TENANT,
                execution_id=executing.execution_id,
                aggregate_type="consumer_placement",
                aggregate_id="placement_1",
                outcome="unknown",
            ),
            audit_events=[_unknown_audit().model_copy(update={"event_id": "audit_stale_unknown"})],
            outbox_records=[outbox],
        )
        duplicate = await ledger.reserve(_reservation("exec_retry"))
        if duplicate.status == "reserved":
            await ledger.execute(duplicate, invoke)

        async with databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, compensation_status, "
                        "(SELECT version FROM merchants WHERE tenant_id = :tenant_id "
                        "AND merchant_id = 'demo-m001'), "
                        "(SELECT COUNT(*) FROM audit_events), (SELECT COUNT(*) FROM outbox) "
                        "FROM tool_executions WHERE execution_id = 'exec_stale'"
                    ),
                    {"tenant_id": TENANT},
                )
            ).one()

    assert unknown.status == duplicate.status == "unknown"
    assert row == ("unknown", "reconciliation_required", 2, 1, 1)
    assert adapter_calls == 0


@pytest.mark.asyncio
async def test_fresh_executing_remains_waiting_without_projection_or_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    clock = _Clock()

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(
            databases.business_sessions,
            clock=clock,
            executing_timeout=timedelta(minutes=5),
        )
        executing = await ledger.mark_executing(await ledger.reserve(_reservation("exec_waiting")))
        waiting = await ledger.recover_stale_executing(executing)
        async with databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, compensation_status, (SELECT COUNT(*) FROM audit_events), "
                        "(SELECT COUNT(*) FROM outbox) FROM tool_executions "
                        "WHERE execution_id = 'exec_waiting'"
                    )
                )
            ).one()

    assert waiting.status == "executing"
    assert row == ("executing", None, 0, 0)
