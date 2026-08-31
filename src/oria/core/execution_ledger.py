"""Business DB transaction boundary for idempotent external side effects."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import canonical_args_hash
from oria.core.types import EventEnvelope
from oria.domain.ledger import DomainEvent, OutboxRecord, Receipt, ToolExecution
from oria.storage.ledger import (
    LedgerRepositoryError,
    SQLiteBusinessAuditRepository,
    SQLiteDomainEventRepository,
    SQLiteOutboxRepository,
    SQLiteToolExecutionRepository,
)

BusinessMutation: TypeAlias = Callable[[AsyncSession], Awaitable[None]]
ExternalInvocation: TypeAlias = Callable[[str], Awaitable[Receipt]]
ExecutionOutcome: TypeAlias = Literal["succeeded", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class ExecutionEventBundle:
    domain_events: Sequence[DomainEvent] = ()
    audit_events: Sequence[EventEnvelope] = ()
    outbox_records: Sequence[OutboxRecord] = ()


OutcomeEventFactory: TypeAlias = Callable[[ExecutionOutcome], ExecutionEventBundle]

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class SideEffectUnknownError(RuntimeError):
    """An adapter cannot determine whether the external side effect happened."""

    def __init__(self, *, receipt_id: str | None = None) -> None:
        super().__init__("external side effect outcome is unknown")
        self.receipt_id = receipt_id


class ExecutionLedger:
    """Reserve, execute, and atomically commit Business DB outcome facts.

    The service owns only a Business DB session factory. Platform coordination is
    intentionally absent; cross-database consistency is recovered through IDs,
    idempotent event consumption, and reconciliation.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._tools = SQLiteToolExecutionRepository(sessions)
        self._domain_events = SQLiteDomainEventRepository(sessions)
        self._audit_events = SQLiteBusinessAuditRepository(sessions)
        self._outbox = SQLiteOutboxRepository(sessions)

    @staticmethod
    def build_idempotency_key(stable_business_id: str, args_hash: str) -> str:
        if not stable_business_id:
            raise ValueError("stable business identity is required")
        if _HASH_PATTERN.fullmatch(args_hash) is None:
            raise ValueError("canonical argument hash is invalid")
        return f"{stable_business_id}:{args_hash}"

    async def reserve_for_args(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        tool_name: str,
        tool_schema_version: int,
        schema: type[BaseModel],
        args: Mapping[str, object],
        stable_business_id: str,
        checkpoint_id: str,
        request_idempotency_key: str | None = None,
        created_at: datetime | None = None,
    ) -> ToolExecution:
        """Validate/hash tool args and atomically reserve their stable idempotency key."""
        args_hash = canonical_args_hash(
            tool_name=tool_name,
            tool_schema_version=tool_schema_version,
            schema=schema,
            args=args,
        )
        now = self._now() if created_at is None else created_at
        reservation = ToolExecution(
            execution_id=execution_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
            idempotency_key=self.build_idempotency_key(stable_business_id, args_hash),
            canonical_args_hash=args_hash,
            checkpoint_id=checkpoint_id,
            created_at=now,
            updated_at=now,
        )
        if request_idempotency_key is not None:
            return await self._tools.reserve_for_request(
                reservation,
                request_idempotency_key,
            )
        return await self.reserve(reservation)

    async def reserve(self, reservation: ToolExecution) -> ToolExecution:
        """Read history first, then reserve; duplicates return the durable record."""
        history = await self._tools.get_by_idempotency(
            reservation.tenant_id,
            reservation.tool_name,
            reservation.idempotency_key,
        )
        if history is not None:
            return history
        return await self._tools.reserve(reservation)

    async def mark_executing(self, execution: ToolExecution) -> ToolExecution:
        return await self._tools.mark_executing(
            execution.tenant_id,
            execution.execution_id,
            self._now(),
        )

    async def record_success(
        self,
        execution: ToolExecution,
        receipt_id: str,
        *,
        business_write: BusinessMutation | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        return await self._commit_outcome(
            execution,
            "succeeded",
            expected_status="executing",
            receipt_id=receipt_id,
            business_write=business_write,
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )

    async def record_local_success(
        self,
        execution: ToolExecution,
        receipt_id: str,
        *,
        business_write: BusinessMutation,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        """Commit a pure database mutation without exposing an executing crash window."""
        if execution.status != "reserved":
            raise ValueError("local execution requires a reserved ledger value")
        self._validate_tenant_bundle(
            execution,
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )
        try:
            async with self._sessions.begin() as session:
                await self._tools._transition(
                    session,
                    tenant_id=execution.tenant_id,
                    execution_id=execution.execution_id,
                    target="executing",
                    expected_status="reserved",
                    updated_at=self._now(),
                )
                await business_write(session)
                succeeded = await self._tools._transition(
                    session,
                    tenant_id=execution.tenant_id,
                    execution_id=execution.execution_id,
                    target="succeeded",
                    expected_status="executing",
                    updated_at=self._now(),
                    receipt_id=receipt_id,
                )
                for domain_event in domain_events:
                    await self._domain_events._append(session, domain_event)
                for audit_event in audit_events:
                    await self._audit_events._append(session, audit_event)
                for outbox_record in outbox_records:
                    await self._outbox._append(session, outbox_record)
                return succeeded
        except (LookupError, ValueError, LedgerRepositoryError):
            raise
        except IntegrityError as exc:
            raise ValueError("business outcome event already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("business execution outcome persistence failed") from exc

    async def record_failure(
        self,
        execution: ToolExecution,
        *,
        compensation_status: str | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        return await self._commit_outcome(
            execution,
            "failed",
            expected_status="executing",
            compensation_status=compensation_status,
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )

    async def record_unknown(
        self,
        execution: ToolExecution,
        *,
        receipt_id: str | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        return await self._commit_outcome(
            execution,
            "unknown",
            expected_status="executing",
            receipt_id=receipt_id,
            compensation_status="reconciliation_required",
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )

    async def reconcile(
        self,
        execution: ToolExecution,
        outcome: Literal["succeeded", "failed"],
        *,
        receipt_id: str | None = None,
        compensation_status: str | None = None,
        business_write: BusinessMutation | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        """Converge unknown once; it never invokes the external adapter again."""
        return await self._commit_outcome(
            execution,
            outcome,
            expected_status="unknown",
            receipt_id=receipt_id,
            compensation_status=compensation_status,
            business_write=business_write,
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )

    async def execute(
        self,
        reservation: ToolExecution,
        invoke: ExternalInvocation,
        *,
        business_write: BusinessMutation | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
        outcome_events: OutcomeEventFactory | None = None,
    ) -> ToolExecution:
        """Run one reserved side effect; retries return history without reinvocation."""
        if reservation.status != "reserved":
            raise ValueError("external execution requires a reserved ledger value")
        history = await self._tools.get_by_idempotency(
            reservation.tenant_id,
            reservation.tool_name,
            reservation.idempotency_key,
        )
        if history is not None and (
            history.execution_id != reservation.execution_id or history.status != "reserved"
        ):
            return history
        if history is None:
            reserved, created = await self._tools._reserve_with_status(reservation)
            if not created:
                return reserved
        else:
            reserved = history
        executing = await self.mark_executing(reserved)
        try:
            receipt = await invoke(executing.idempotency_key)
        except SideEffectUnknownError as exc:
            events = self._outcome_events(
                "unknown",
                outcome_events,
                domain_events,
                audit_events,
                outbox_records,
            )
            return await self.record_unknown(
                executing,
                receipt_id=exc.receipt_id,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
        except Exception:
            events = self._outcome_events(
                "failed",
                outcome_events,
                domain_events,
                audit_events,
                outbox_records,
            )
            await self.record_failure(
                executing,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
            raise
        if receipt.status == "accepted":
            events = self._outcome_events(
                "succeeded",
                outcome_events,
                domain_events,
                audit_events,
                outbox_records,
            )
            return await self.record_success(
                executing,
                receipt.receipt_id,
                business_write=business_write,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
        if receipt.status == "unknown":
            events = self._outcome_events(
                "unknown",
                outcome_events,
                domain_events,
                audit_events,
                outbox_records,
            )
            return await self.record_unknown(
                executing,
                receipt_id=receipt.receipt_id,
                domain_events=events.domain_events,
                audit_events=events.audit_events,
                outbox_records=events.outbox_records,
            )
        events = self._outcome_events(
            "failed",
            outcome_events,
            domain_events,
            audit_events,
            outbox_records,
        )
        return await self.record_failure(
            executing,
            domain_events=events.domain_events,
            audit_events=events.audit_events,
            outbox_records=events.outbox_records,
        )

    @staticmethod
    def _outcome_events(
        outcome: ExecutionOutcome,
        factory: OutcomeEventFactory | None,
        domain_events: Sequence[DomainEvent],
        audit_events: Sequence[EventEnvelope],
        outbox_records: Sequence[OutboxRecord],
    ) -> ExecutionEventBundle:
        if factory is not None:
            return factory(outcome)
        return ExecutionEventBundle(domain_events, audit_events, outbox_records)

    async def _commit_outcome(
        self,
        execution: ToolExecution,
        outcome: Literal["succeeded", "failed", "unknown"],
        *,
        expected_status: Literal["executing", "unknown"],
        receipt_id: str | None = None,
        compensation_status: str | None = None,
        business_write: BusinessMutation | None = None,
        domain_events: Sequence[DomainEvent] = (),
        audit_events: Sequence[EventEnvelope] = (),
        outbox_records: Sequence[OutboxRecord] = (),
    ) -> ToolExecution:
        self._validate_tenant_bundle(
            execution,
            domain_events=domain_events,
            audit_events=audit_events,
            outbox_records=outbox_records,
        )
        if outcome != "succeeded" and business_write is not None:
            raise ValueError("business state writes require a confirmed successful outcome")
        try:
            async with self._sessions.begin() as session:
                if business_write is not None:
                    await business_write(session)
                transitioned = await self._tools._transition(
                    session,
                    tenant_id=execution.tenant_id,
                    execution_id=execution.execution_id,
                    target=outcome,
                    expected_status=expected_status,
                    updated_at=self._now(),
                    receipt_id=receipt_id,
                    compensation_status=compensation_status,
                )
                for domain_event in domain_events:
                    await self._domain_events._append(session, domain_event)
                for audit_event in audit_events:
                    await self._audit_events._append(session, audit_event)
                for outbox_record in outbox_records:
                    await self._outbox._append(session, outbox_record)
                return transitioned
        except (LookupError, ValueError, LedgerRepositoryError):
            raise
        except IntegrityError as exc:
            raise ValueError("business outcome event already exists") from exc
        except SQLAlchemyError as exc:
            raise LedgerRepositoryError("business execution outcome persistence failed") from exc

    @staticmethod
    def _validate_tenant_bundle(
        execution: ToolExecution,
        *,
        domain_events: Sequence[DomainEvent],
        audit_events: Sequence[EventEnvelope],
        outbox_records: Sequence[OutboxRecord],
    ) -> None:
        tenant_id = execution.tenant_id
        if any(event.tenant_id != tenant_id for event in domain_events):
            raise ValueError("cross-tenant domain event is forbidden")
        if any(
            event.tenant_id != tenant_id or event.resource.tenant_id != tenant_id
            for event in audit_events
        ):
            raise ValueError("cross-tenant business audit is forbidden")
        if any(record.tenant_id != tenant_id for record in outbox_records):
            raise ValueError("cross-tenant outbox event is forbidden")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution ledger clock must return timezone-aware values")
        return value
