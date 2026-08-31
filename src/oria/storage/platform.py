"""Tenant-scoped SQLite repositories for V0.3 platform coordination records."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import Approval
from oria.core.integration_events import ExternalWait, IntegrationInboxRecord
from oria.core.types import EventEnvelope
from oria.permission.audit import redact_audit_payload


class PlatformRepositoryError(RuntimeError):
    """Safe platform repository failure without SQL or event payload contents."""


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
                        "updated_at, decided_at) VALUES (:tenant_id, :approval_id, "
                        ":approval_action, :tool_name, :canonical_args_hash, :checkpoint_id, "
                        ":policy_version, :expires_at, :status, :requester, :decider, :decision, "
                        ":reason, :created_at, :updated_at, :decided_at)"
                    ),
                    approval.model_dump(),
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
                        "FROM approvals WHERE tenant_id = :tenant_id AND approval_id = :approval_id"
                    ),
                    {"tenant_id": tenant_id, "approval_id": approval_id},
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval read failed") from exc
        return None if row is None else Approval.model_validate(dict(row))

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
                    approval.model_dump(),
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
