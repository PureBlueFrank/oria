"""Tenant-scoped SQLite repositories for V0.3 platform coordination records."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import (
    Approval,
    ApprovalBindingInvalidationFact,
    ApprovalBusinessBinding,
    ApprovalInvalidationStatus,
)
from oria.core.integration_events import (
    ConsumedIntegrationInbox,
    ExternalWait,
    IntegrationEventEnvelope,
    IntegrationInboxIdentity,
    IntegrationInboxRecord,
    integration_payload_hash,
)
from oria.core.types import EventEnvelope
from oria.permission.audit import redact_audit_payload


class PlatformRepositoryError(RuntimeError):
    """Safe platform repository failure without SQL or event payload contents."""


def _approval_values(approval: Approval) -> dict[str, Any]:
    values = approval.model_dump(exclude={"business_binding"})
    binding = approval.business_binding
    values.update(
        {
            "campaign_id": None if binding is None else binding.campaign_id,
            "enrollment_version": None if binding is None else binding.enrollment_version,
            "link_version": None if binding is None else binding.link_version,
            "selection_version": None if binding is None else binding.selection_version,
            "selection_hash": None if binding is None else binding.selection_hash,
            "rule_snapshot_hash": None if binding is None else binding.rule_snapshot_hash,
        }
    )
    return values


class SQLiteApprovalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(
        self,
        approval: Approval,
        audit_event: EventEnvelope | None = None,
    ) -> None:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO approvals (tenant_id, approval_id, approval_action, "
                        "tool_name, canonical_args_hash, checkpoint_id, policy_version, "
                        "expires_at, status, requester, decider, decision, reason, created_at, "
                        "updated_at, decided_at, campaign_id, enrollment_version, link_version, "
                        "selection_version, selection_hash, rule_snapshot_hash) VALUES "
                        "(:tenant_id, :approval_id, "
                        ":approval_action, :tool_name, :canonical_args_hash, :checkpoint_id, "
                        ":policy_version, :expires_at, :status, :requester, :decider, :decision, "
                        ":reason, :created_at, :updated_at, :decided_at, :campaign_id, "
                        ":enrollment_version, :link_version, :selection_version, :selection_hash, "
                        ":rule_snapshot_hash)"
                    ),
                    _approval_values(approval),
                )
                if audit_event is not None:
                    await self._append_approval_events(session, approval, audit_event)
        except IntegrityError as exc:
            raise ValueError("approval already exists") from exc
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval persistence failed") from exc

    async def get(self, tenant_id: str, approval_id: str) -> Approval | None:
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id, approval_id, approval_action, tool_name, "
                        "canonical_args_hash, checkpoint_id, policy_version, expires_at, status, "
                        "requester, decider, decision, reason, created_at, updated_at, decided_at "
                        ", campaign_id, enrollment_version, link_version, selection_version, "
                        "selection_hash, rule_snapshot_hash "
                        "FROM approvals WHERE tenant_id = :tenant_id AND approval_id = :approval_id"
                    ),
                    {"tenant_id": tenant_id, "approval_id": approval_id},
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval read failed") from exc
        if row is None:
            return None
        values = dict(row)
        campaign_id = values.pop("campaign_id")
        binding_values = {
            "campaign_id": campaign_id,
            "enrollment_version": values.pop("enrollment_version"),
            "link_version": values.pop("link_version"),
            "selection_version": values.pop("selection_version"),
            "selection_hash": values.pop("selection_hash"),
            "rule_snapshot_hash": values.pop("rule_snapshot_hash"),
        }
        values["business_binding"] = (
            None if campaign_id is None else ApprovalBusinessBinding.model_validate(binding_values)
        )
        return Approval.model_validate(values)

    async def replace(
        self,
        approval: Approval,
        audit_event: EventEnvelope | None = None,
    ) -> None:
        try:
            async with self._sessions.begin() as session:
                result = await session.execute(
                    text(
                        "UPDATE approvals SET status = :status, decider = :decider, "
                        "decision = :decision, reason = :reason, updated_at = :updated_at, "
                        "decided_at = :decided_at WHERE tenant_id = :tenant_id "
                        "AND approval_id = :approval_id"
                    ),
                    _approval_values(approval),
                )
                if not isinstance(result, CursorResult) or result.rowcount != 1:
                    raise LookupError("approval is unavailable")
                if audit_event is not None:
                    await self._append_approval_events(session, approval, audit_event)
        except LookupError:
            raise
        except IntegrityError as exc:
            raise ValueError("approval event already exists") from exc
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval update failed") from exc

    async def invalidate_campaign_binding(
        self,
        *,
        tenant_id: str,
        binding: ApprovalBusinessBinding,
        updated_at: datetime,
    ) -> int:
        try:
            async with self._sessions.begin() as session:
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        text(
                            "UPDATE approvals SET status = 'invalidated', updated_at = "
                            ":updated_at WHERE tenant_id = :tenant_id AND campaign_id = "
                            ":campaign_id AND status IN ('pending', 'approved') AND NOT "
                            "(enrollment_version = :enrollment_version AND link_version = "
                            ":link_version AND selection_version = :selection_version AND "
                            "selection_hash IS :selection_hash AND "
                            "rule_snapshot_hash = :rule_snapshot_hash)"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "updated_at": updated_at,
                            **binding.model_dump(),
                        },
                    ),
                )
                return result.rowcount
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("campaign approval invalidation failed") from exc

    async def _append_approval_events(
        self,
        session: AsyncSession,
        approval: Approval,
        event: EventEnvelope,
    ) -> None:
        if (
            event.tenant_id != approval.tenant_id
            or event.resource.tenant_id != approval.tenant_id
            or event.resource.resource_type != "approval"
            or event.resource.resource_id != approval.approval_id
        ):
            raise ValueError("approval event binding is invalid")
        sanitized_payload = redact_audit_payload(event.payload)
        payload_json = json.dumps(
            sanitized_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await session.execute(
            text(
                "INSERT INTO audit_events (event_id, occurred_at, tenant_id, actor, action, "
                "resource_type, resource_id, resource_tenant_id, decision, policy_version, "
                "args_hash, result, correlation_id, payload_json) VALUES (:event_id, "
                ":occurred_at, :tenant_id, :actor, :action, 'approval', :resource_id, "
                ":tenant_id, :decision, :policy_version, :args_hash, :result, :correlation_id, "
                ":payload_json)"
            ),
            event.model_dump(exclude={"resource", "payload"})
            | {"resource_id": approval.approval_id, "payload_json": payload_json},
        )
        await session.execute(
            text(
                "INSERT INTO outbox (event_id, tenant_id, topic, payload_json, occurred_at, "
                "available_at, published_at, attempt_count, last_error_code) VALUES (:event_id, "
                ":tenant_id, 'platform.approval.status_changed', :payload_json, :occurred_at, "
                ":occurred_at, NULL, 0, NULL)"
            ),
            {
                "event_id": event.event_id,
                "tenant_id": event.tenant_id,
                "payload_json": payload_json,
                "occurred_at": event.occurred_at,
            },
        )


class SQLiteApprovalInvalidationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._approvals = SQLiteApprovalRepository(sessions)

    async def record_pending(
        self,
        fact: ApprovalBindingInvalidationFact,
    ) -> ApprovalInvalidationStatus:
        existing = await self._get(fact)
        if existing is not None:
            return existing
        values = self._fact_values(fact)
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO approval_binding_invalidations (tenant_id, event_id, "
                        "campaign_id, enrollment_version, link_version, selection_version, "
                        "selection_hash, rule_snapshot_hash, reason, status, attempt_count, "
                        "last_error_code, "
                        "created_at, updated_at) VALUES (:tenant_id, :event_id, :campaign_id, "
                        ":enrollment_version, :link_version, :selection_version, :selection_hash, "
                        ":rule_snapshot_hash, :reason, 'pending', 0, NULL, :occurred_at, "
                        ":occurred_at)"
                    ),
                    values,
                )
        except IntegrityError as exc:
            existing = await self._get(fact)
            if existing is None:
                raise PlatformRepositoryError("approval invalidation persistence failed") from exc
            return existing
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval invalidation persistence failed") from exc
        return "pending"

    async def apply(self, fact: ApprovalBindingInvalidationFact) -> int:
        try:
            async with self._sessions.begin() as session:
                count = await self._invalidate(session, fact)
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        text(
                            "UPDATE approval_binding_invalidations SET status = 'applied', "
                            "attempt_count = attempt_count + 1, last_error_code = NULL, "
                            "updated_at = :updated_at WHERE tenant_id = :tenant_id AND event_id = "
                            ":event_id AND status IN ('pending', 'reconciliation')"
                        ),
                        {
                            "tenant_id": fact.tenant_id,
                            "event_id": fact.event_id,
                            "updated_at": fact.occurred_at,
                        },
                    ),
                )
                if result.rowcount != 1:
                    status = await self._get_in_session(session, fact)
                    if status != "applied":
                        raise PlatformRepositoryError("approval invalidation is unavailable")
                return count
        except PlatformRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval invalidation apply failed") from exc

    async def mark_reconciliation(
        self,
        fact: ApprovalBindingInvalidationFact,
        *,
        error_code: str,
    ) -> None:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE approval_binding_invalidations SET status = 'reconciliation', "
                        "attempt_count = attempt_count + 1, last_error_code = :error_code, "
                        "updated_at = :updated_at WHERE tenant_id = :tenant_id AND event_id = "
                        ":event_id AND status != 'applied'"
                    ),
                    {
                        "tenant_id": fact.tenant_id,
                        "event_id": fact.event_id,
                        "error_code": error_code,
                        "updated_at": fact.occurred_at,
                    },
                )
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval invalidation reconciliation failed") from exc

    async def _invalidate(
        self,
        session: AsyncSession,
        fact: ApprovalBindingInvalidationFact,
    ) -> int:
        binding = fact.binding
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE approvals SET status = 'invalidated', updated_at = :updated_at "
                    "WHERE tenant_id = :tenant_id AND campaign_id = :campaign_id AND status IN "
                    "('pending', 'approved') AND NOT (enrollment_version = :enrollment_version "
                    "AND link_version = :link_version AND selection_version = "
                    ":selection_version AND selection_hash IS :selection_hash AND "
                    "rule_snapshot_hash = :rule_snapshot_hash)"
                ),
                {
                    "tenant_id": fact.tenant_id,
                    "updated_at": fact.occurred_at,
                    **binding.model_dump(),
                },
            ),
        )
        return result.rowcount

    async def _get(
        self, fact: ApprovalBindingInvalidationFact
    ) -> ApprovalInvalidationStatus | None:
        try:
            async with self._sessions() as session:
                return await self._get_in_session(session, fact)
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval invalidation read failed") from exc

    async def _get_in_session(
        self,
        session: AsyncSession,
        fact: ApprovalBindingInvalidationFact,
    ) -> ApprovalInvalidationStatus | None:
        result = await session.execute(
            text(
                "SELECT campaign_id, enrollment_version, link_version, selection_version, "
                "selection_hash, rule_snapshot_hash, reason, status FROM "
                "approval_binding_invalidations WHERE "
                "tenant_id = :tenant_id AND event_id = :event_id"
            ),
            {"tenant_id": fact.tenant_id, "event_id": fact.event_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        expected = {
            **fact.binding.model_dump(),
            "reason": fact.reason,
        }
        observed = {name: row[name] for name in expected}
        if observed != expected:
            raise ValueError("approval invalidation event conflicts with persisted payload")
        return cast(ApprovalInvalidationStatus, row["status"])

    @staticmethod
    def _fact_values(fact: ApprovalBindingInvalidationFact) -> dict[str, Any]:
        return {
            "tenant_id": fact.tenant_id,
            "event_id": fact.event_id,
            "reason": fact.reason,
            "occurred_at": fact.occurred_at,
            **fact.binding.model_dump(),
        }


class SQLiteIntegrationEventInboxRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add_wait(self, wait: ExternalWait) -> None:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO external_waits (tenant_id, wait_id, event_type, "
                        "resource_type, resource_id, expected_version, checkpoint_id, expires_at, "
                        "timeout_action, status, created_at, resolved_at) VALUES (:tenant_id, "
                        ":wait_id, :event_type, :resource_type, :resource_id, :expected_version, "
                        ":checkpoint_id, :expires_at, :timeout_action, :status, :created_at, "
                        ":resolved_at)"
                    ),
                    wait.model_dump(),
                )
        except IntegrityError as exc:
            raise ValueError("external wait already exists") from exc
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("external wait persistence failed") from exc

    async def get_wait(self, tenant_id: str, wait_id: str) -> ExternalWait | None:
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id, wait_id, event_type, resource_type, resource_id, "
                        "expected_version, checkpoint_id, expires_at, timeout_action, status, "
                        "created_at, resolved_at FROM external_waits WHERE tenant_id = "
                        ":tenant_id AND wait_id = :wait_id"
                    ),
                    {"tenant_id": tenant_id, "wait_id": wait_id},
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("external wait read failed") from exc
        return None if row is None else ExternalWait.model_validate(dict(row))

    async def add(self, record: IntegrationInboxRecord) -> bool:
        values: dict[str, Any] = record.model_dump(exclude={"redacted_payload"})
        values["redacted_payload_json"] = json.dumps(
            record.redacted_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO integration_event_inbox (tenant_id, adapter_id, "
                        "source_event_id, schema_version, event_type, resource_version, "
                        "signature_subject, redacted_payload_json, payload_hash, "
                        "processing_status, wait_id, received_at, processed_at) VALUES "
                        "(:tenant_id, :adapter_id, :source_event_id, :schema_version, :event_type, "
                        ":resource_version, :signature_subject, :redacted_payload_json, "
                        ":payload_hash, :processing_status, :wait_id, :received_at, :processed_at)"
                    ),
                    values,
                )
        except IntegrityError as exc:
            if await self._exists(record):
                return False
            raise PlatformRepositoryError("integration event persistence failed") from exc
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("integration event persistence failed") from exc
        return True

    async def get(
        self,
        tenant_id: str,
        adapter_id: str,
        source_event_id: str,
    ) -> IntegrationInboxRecord | None:
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id, adapter_id, source_event_id, schema_version, "
                        "event_type, resource_version, signature_subject, redacted_payload_json, "
                        "payload_hash, processing_status, wait_id, received_at, processed_at "
                        "FROM integration_event_inbox WHERE tenant_id = :tenant_id "
                        "AND adapter_id = :adapter_id AND source_event_id = :source_event_id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "adapter_id": adapter_id,
                        "source_event_id": source_event_id,
                    },
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("integration event read failed") from exc
        if row is None:
            return None
        values = dict(row)
        values["redacted_payload"] = json.loads(values.pop("redacted_payload_json"))
        return IntegrationInboxRecord.model_validate(values)

    async def consume_matched(
        self,
        identity: IntegrationInboxIdentity,
        event: IntegrationEventEnvelope,
        *,
        consumed_at: datetime,
    ) -> ConsumedIntegrationInbox:
        if consumed_at.tzinfo is None or consumed_at.utcoffset() is None:
            raise ValueError("inbox consumption time must include a timezone")
        try:
            async with self._sessions.begin() as session:
                result = await session.execute(
                    text(
                        "SELECT i.tenant_id, i.adapter_id, i.source_event_id, i.schema_version, "
                        "i.event_type, i.resource_version, i.signature_subject, "
                        "i.redacted_payload_json, i.payload_hash, i.processing_status, i.wait_id, "
                        "i.received_at, i.processed_at, w.event_type AS wait_event_type, "
                        "w.resource_type, w.resource_id, w.expected_version, w.checkpoint_id, "
                        "w.expires_at, w.timeout_action, w.status AS wait_status, "
                        "w.created_at AS wait_created_at, w.resolved_at FROM "
                        "integration_event_inbox AS i JOIN external_waits AS w ON w.tenant_id = "
                        "i.tenant_id AND w.wait_id = i.wait_id WHERE i.tenant_id = :tenant_id "
                        "AND i.adapter_id = :adapter_id AND i.source_event_id = :source_event_id"
                    ),
                    identity.model_dump(),
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise PermissionError("selection event is not present in the trusted inbox")
                record = IntegrationInboxRecord.model_validate(
                    {
                        "tenant_id": row["tenant_id"],
                        "adapter_id": row["adapter_id"],
                        "source_event_id": row["source_event_id"],
                        "schema_version": row["schema_version"],
                        "event_type": row["event_type"],
                        "resource_version": row["resource_version"],
                        "signature_subject": row["signature_subject"],
                        "redacted_payload": json.loads(row["redacted_payload_json"]),
                        "payload_hash": row["payload_hash"],
                        "processing_status": row["processing_status"],
                        "wait_id": row["wait_id"],
                        "received_at": row["received_at"],
                        "processed_at": row["processed_at"],
                    }
                )
                wait = ExternalWait.model_validate(
                    {
                        "tenant_id": row["tenant_id"],
                        "wait_id": row["wait_id"],
                        "event_type": row["wait_event_type"],
                        "resource_type": row["resource_type"],
                        "resource_id": row["resource_id"],
                        "expected_version": row["expected_version"],
                        "checkpoint_id": row["checkpoint_id"],
                        "expires_at": row["expires_at"],
                        "timeout_action": row["timeout_action"],
                        "status": row["wait_status"],
                        "created_at": row["wait_created_at"],
                        "resolved_at": row["resolved_at"],
                    }
                )
                if (
                    identity.tenant_id != event.tenant_id
                    or record.tenant_id != event.tenant_id
                    or record.adapter_id != event.adapter_id
                    or record.source_event_id != event.source_event_id
                    or record.schema_version != event.schema_version
                    or record.event_type != event.event_type
                    or record.resource_version != event.version
                    or record.signature_subject != event.signature_subject
                    or record.payload_hash != integration_payload_hash(event)
                    or record.processing_status != "matched"
                    or wait.status != "waiting"
                    or wait.event_type != event.event_type
                    or wait.resource_type != "campaign"
                    or wait.resource_id != event.payload.campaign_id
                    or wait.expected_version != event.version
                ):
                    raise PermissionError("selection event does not match its persisted inbox wait")
                inbox_update = cast(
                    CursorResult[Any],
                    await session.execute(
                        text(
                            "UPDATE integration_event_inbox SET processing_status = 'consumed', "
                            "processed_at = :consumed_at WHERE tenant_id = :tenant_id AND "
                            "adapter_id = :adapter_id AND source_event_id = :source_event_id AND "
                            "processing_status = 'matched' AND wait_id = :wait_id"
                        ),
                        identity.model_dump()
                        | {"consumed_at": consumed_at, "wait_id": wait.wait_id},
                    ),
                )
                wait_update = cast(
                    CursorResult[Any],
                    await session.execute(
                        text(
                            "UPDATE external_waits SET status = 'matched', resolved_at = "
                            ":consumed_at WHERE tenant_id = :tenant_id AND wait_id = :wait_id "
                            "AND status = 'waiting' AND checkpoint_id = :checkpoint_id"
                        ),
                        {
                            "tenant_id": identity.tenant_id,
                            "wait_id": wait.wait_id,
                            "checkpoint_id": wait.checkpoint_id,
                            "consumed_at": consumed_at,
                        },
                    ),
                )
                if inbox_update.rowcount != 1 or wait_update.rowcount != 1:
                    raise PermissionError("selection inbox event was already consumed")
                return ConsumedIntegrationInbox(
                    record=record.model_copy(
                        update={"processing_status": "consumed", "processed_at": consumed_at}
                    ),
                    wait=wait.model_copy(update={"status": "matched", "resolved_at": consumed_at}),
                )
        except PermissionError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise PlatformRepositoryError("integration event consumption failed") from exc

    async def _exists(self, record: IntegrationInboxRecord) -> bool:
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT 1 FROM integration_event_inbox WHERE tenant_id = :tenant_id "
                        "AND adapter_id = :adapter_id AND source_event_id = :source_event_id"
                    ),
                    {
                        "tenant_id": record.tenant_id,
                        "adapter_id": record.adapter_id,
                        "source_event_id": record.source_event_id,
                    },
                )
                return result.first() is not None
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("integration event duplicate check failed") from exc
