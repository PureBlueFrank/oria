"""Contracts for reserve-first execution and the Business DB transaction boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import oria.domain.ledger as _domain_ledger  # noqa: F401  # Initialize domain package first.
from oria.config import resolve_runtime_config
from oria.core.execution_ledger import ExecutionLedger
from oria.core.types import EventEnvelope, ResourceRef
from oria.data import initialize_data
from oria.domain.ledger import DomainEvent, OutboxRecord, Receipt, ToolExecution
from oria.storage.database import DatabaseResources
from oria.storage.ledger import SQLiteDomainEventRepository

pytestmark = pytest.mark.contract

NOW = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
HASH_A = f"sha256:{'a' * 64}"
HASH_B = f"sha256:{'b' * 64}"
TENANT = "local-community"


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


class _ToolArgs(BaseModel):
    campaign_id: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class _NoopOutcomeProjection:
    tenant_id: str
    execution_id: str
    aggregate_type: str
    aggregate_id: str
    outcome: Literal["failed", "unknown"]

    async def apply(self, session: AsyncSession) -> None:
        del session


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


def _reservation(execution_id: str = "exec_1") -> ToolExecution:
    return ToolExecution(
        execution_id=execution_id,
        tenant_id=TENANT,
        tool_name="publish_recruitment",
        idempotency_key=f"campaign_1:{HASH_A}",
        canonical_args_hash=HASH_A,
        checkpoint_id="checkpoint_1",
        created_at=NOW,
        updated_at=NOW,
    )


def _domain_event(event_id: str = "event_1") -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        tenant_id=TENANT,
        aggregate_type="campaign",
        aggregate_id="campaign_1",
        event_type="campaign.recruitment_published",
        event_version=1,
        payload={"receipt_hash": HASH_B},
        occurred_at=NOW,
        correlation_id="correlation_1",
    )


def _audit_event(event_id: str = "audit_1") -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        occurred_at=NOW,
        tenant_id=TENANT,
        actor="operator_1",
        action="publish_recruitment",
        resource=ResourceRef(
            resource_type="campaign",
            resource_id="campaign_1",
            tenant_id=TENANT,
        ),
        decision="allow",
        policy_version="policy_v1",
        args_hash=HASH_A,
        result="success",
        correlation_id="correlation_1",
        payload={"receipt_hash": HASH_B},
    )


def _outbox(event_id: str = "event_1") -> OutboxRecord:
    return OutboxRecord(
        event_id=event_id,
        tenant_id=TENANT,
        topic="campaign.recruitment_published",
        payload_json=f'{{"campaign_id":"campaign_1","receipt_hash":"{HASH_B}"}}',
        occurred_at=NOW,
        available_at=NOW,
    )


@pytest.mark.asyncio
async def test_execute_reserves_before_invocation_and_duplicate_returns_history(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    calls: list[str] = []

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())

        async def invoke(idempotency_key: str) -> Receipt:
            calls.append(idempotency_key)
            async with databases.business_sessions() as session:
                status = await session.scalar(
                    text(
                        "SELECT status FROM tool_executions WHERE tenant_id = :tenant_id "
                        "AND idempotency_key = :idempotency_key"
                    ),
                    {"tenant_id": TENANT, "idempotency_key": idempotency_key},
                )
            assert status == "executing"
            return Receipt(
                receipt_id="receipt_1",
                adapter_id="mock_recruitment",
                resource_ref="campaign:campaign_1",
                status="accepted",
                received_at=NOW + timedelta(seconds=1),
                summary_hash=HASH_B,
            )

        async def update_business_state(session: AsyncSession) -> None:
            await session.execute(
                text(
                    "UPDATE merchants SET version = version + 1, updated_at = :updated_at "
                    "WHERE tenant_id = :tenant_id AND merchant_id = 'demo-m001'"
                ),
                {"tenant_id": TENANT, "updated_at": NOW + timedelta(seconds=2)},
            )

        first = await ledger.execute(
            _reservation(),
            invoke,
            business_write=update_business_state,
            domain_events=[_domain_event()],
            audit_events=[_audit_event()],
            outbox_records=[_outbox()],
        )
        duplicate = await ledger.execute(_reservation("exec_retry"), invoke)

        async with databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM tool_executions), "
                        "(SELECT COUNT(*) FROM domain_events), "
                        "(SELECT COUNT(*) FROM audit_events), "
                        "(SELECT COUNT(*) FROM outbox), "
                        "(SELECT version FROM merchants WHERE tenant_id = :tenant_id "
                        "AND merchant_id = 'demo-m001')"
                    ),
                    {"tenant_id": TENANT},
                )
            ).one()

    assert first.status == duplicate.status == "succeeded"
    assert first.execution_id == duplicate.execution_id == "exec_1"
    assert calls == [f"campaign_1:{HASH_A}"]
    assert counts == (1, 1, 1, 1, 2)


@pytest.mark.asyncio
async def test_reserve_for_args_uses_canonical_hash_in_a_stable_idempotency_key(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())
        first = await ledger.reserve_for_args(
            execution_id="exec_args_1",
            tenant_id=TENANT,
            tool_name="materialize_coupon_batch",
            tool_schema_version=1,
            schema=_ToolArgs,
            args={"campaign_id": "campaign_1", "amount": Decimal("10.00")},
            stable_business_id="campaign_1:coupon",
            checkpoint_id="checkpoint_1",
        )
        duplicate = await ledger.reserve_for_args(
            execution_id="exec_args_2",
            tenant_id=TENANT,
            tool_name="materialize_coupon_batch",
            tool_schema_version=1,
            schema=_ToolArgs,
            args={"amount": Decimal("10.0"), "campaign_id": "campaign_1"},
            stable_business_id="campaign_1:coupon",
            checkpoint_id="checkpoint_retry",
        )

    assert first.execution_id == duplicate.execution_id == "exec_args_1"
    assert first.idempotency_key == f"campaign_1:coupon:{first.canonical_args_hash}"


@pytest.mark.asyncio
async def test_execute_invokes_an_explicit_reserve_for_args_record_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    calls = 0

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())
        reserved = await ledger.reserve_for_args(
            execution_id="exec_reserved_args",
            tenant_id=TENANT,
            tool_name="materialize_coupon_batch",
            tool_schema_version=1,
            schema=_ToolArgs,
            args={"campaign_id": "campaign_1", "amount": Decimal("10")},
            stable_business_id="campaign_1:coupon",
            checkpoint_id="checkpoint_1",
        )

        async def invoke(_: str) -> Receipt:
            nonlocal calls
            calls += 1
            return Receipt(
                receipt_id="receipt_reserved_args",
                adapter_id="mock_coupon",
                resource_ref="coupon:coupon_1",
                status="accepted",
                received_at=NOW + timedelta(seconds=1),
                summary_hash=HASH_B,
            )

        first = await ledger.execute(reserved, invoke)
        duplicate = await ledger.execute(reserved, invoke)

    assert first.status == duplicate.status == "succeeded"
    assert calls == 1


@pytest.mark.asyncio
async def test_business_state_ledger_and_events_roll_back_as_one_transaction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())
        executing = await ledger.mark_executing(await ledger.reserve(_reservation()))
        await SQLiteDomainEventRepository(databases.business_sessions).append(_domain_event())

        async def update_business_state(session: AsyncSession) -> None:
            await session.execute(
                text(
                    "UPDATE merchants SET version = version + 1 WHERE tenant_id = :tenant_id "
                    "AND merchant_id = 'demo-m001'"
                ),
                {"tenant_id": TENANT},
            )

        with pytest.raises(ValueError, match="already exists"):
            await ledger.record_success(
                executing,
                "receipt_1",
                business_write=update_business_state,
                domain_events=[_domain_event()],
                outbox_records=[_outbox()],
            )

        async with databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, (SELECT version FROM merchants WHERE tenant_id = "
                        ":tenant_id AND merchant_id = 'demo-m001') AS merchant_version "
                        "FROM tool_executions WHERE execution_id = 'exec_1'"
                    ),
                    {"tenant_id": TENANT},
                )
            ).one()
            outbox_count = await session.scalar(text("SELECT COUNT(*) FROM outbox"))

    assert row == ("executing", 1)
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_failed_outcomes_reject_business_writes_and_validate_narrow_projection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions, clock=_Clock())
        executing = await ledger.mark_executing(await ledger.reserve(_reservation("exec_guard")))

        async def forbidden_success_write(session: AsyncSession) -> None:
            await session.execute(
                text(
                    "UPDATE merchants SET version = version + 1 WHERE tenant_id = :tenant_id "
                    "AND merchant_id = 'demo-m001'"
                ),
                {"tenant_id": TENANT},
            )

        with pytest.raises(
            ValueError, match="business state writes require a confirmed successful outcome"
        ):
            await ledger.record_failure(executing, business_write=forbidden_success_write)
        with pytest.raises(ValueError, match="execution binding does not match"):
            await ledger.record_failure(
                executing,
                outcome_projection=_NoopOutcomeProjection(
                    tenant_id=TENANT,
                    execution_id="another-execution",
                    aggregate_type="consumer_placement",
                    aggregate_id="placement-a",
                    outcome="failed",
                ),
            )
        failed = await ledger.record_failure(
            executing,
            outcome_projection=_NoopOutcomeProjection(
                tenant_id=TENANT,
                execution_id=executing.execution_id,
                aggregate_type="consumer_placement",
                aggregate_id="placement-a",
                outcome="failed",
            ),
        )
        async with databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, (SELECT version FROM merchants WHERE tenant_id = "
                        ":tenant_id AND merchant_id = 'demo-m001') FROM tool_executions "
                        "WHERE execution_id = 'exec_guard'"
                    ),
                    {"tenant_id": TENANT},
                )
            ).one()

    assert failed.status == "failed"
    assert row == ("failed", 1)
