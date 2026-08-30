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


class PlatformRepositoryError(RuntimeError):
    """Safe platform repository failure without SQL or event payload contents."""


class SQLiteApprovalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, approval: Approval) -> None:
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

    async def replace(self, approval: Approval) -> None:
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
        except LookupError:
            raise
        except SQLAlchemyError as exc:
            raise PlatformRepositoryError("approval update failed") from exc


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
