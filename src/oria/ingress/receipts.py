"""Atomic durable ingress acceptance; never acknowledges before business commit."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.api.identity import AuthenticationError, IdentityDirectory
from oria.ingress.feishu import VerifiedFeishuEvent
from oria.storage.database import set_session_tenant_context


class IngressConflict(ValueError):
    """A replay/source identity was reused for different normalized content."""


class IngressUnavailable(RuntimeError):
    """Safe storage failure; the caller must not acknowledge acceptance."""


class TransactionalAcceptor(Protocol):
    """Trusted service implementation, not an arbitrary callback or network tool.

    authorize checks current Policy and resolves action_ref against server-owned
    resource/version/wait. persist writes the accepted intent/job/decision using
    the provided transaction only: no commit, separate connection, or external I/O.
    This is an internal seam; the session is never exposed to agents or clients.
    """

    async def authorize(self, session: AsyncSession, event: VerifiedFeishuEvent) -> None: ...

    async def persist(
        self, session: AsyncSession, event: VerifiedFeishuEvent, receipt_ref: str
    ) -> None: ...


def _content_hash(event: VerifiedFeishuEvent) -> str:
    message = event.message
    content = {
        "kind": event.kind,
        "issuer": event.binding.issuer,
        "subject": event.binding.subject,
        "tenant": message.mapped_tenant_id,
        "actor": message.mapped_subject_id,
        "source_id": message.source_message_id,
        "sender": message.sender_ref,
        "target": message.target_ref,
        "text": message.text,
        "action": event.action.model_dump() if event.action else None,
    }
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TransactionalFeishuReceiver:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        directory: Callable[[], IdentityDirectory],
        acceptor: TransactionalAcceptor,
    ) -> None:
        self._sessions = sessions
        self._directory = directory
        self._acceptor = acceptor

    async def accept(self, event: VerifiedFeishuEvent) -> None:
        now = datetime.now(UTC)
        binding = self._directory().resolve(event.binding.issuer, event.binding.subject)
        if (
            binding != event.binding
            or binding.actor.tenant_id != event.message.mapped_tenant_id
            or binding.actor.subject_id != event.message.mapped_subject_id
            or event.expires_at < now.timestamp()
        ):
            raise AuthenticationError("ingress identity changed or expired")
        values = {
            "tenant_id": binding.actor.tenant_id,
            "dedupe_key": event.message.dedupe_key,
            "content_hash": _content_hash(event),
            "receipt_ref": uuid.uuid4().hex,
            "created_at": now,
            "replay_key": event.replay_key,
            "expires_at": event.expires_at,
        }
        try:
            async with self._sessions.begin() as session:
                await set_session_tenant_context(session, binding.actor.tenant_id)
                # INSERT first serializes duplicate writers; rollback also releases the claim.
                result = await session.execute(
                    text(
                        "INSERT INTO ingress_receipts "
                        "(tenant_id, dedupe_key, content_hash, receipt_ref, created_at) "
                        "VALUES (:tenant_id, :dedupe_key, :content_hash, "
                        ":receipt_ref, :created_at) "
                        "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING RETURNING receipt_ref"
                    ),
                    values,
                )
                created = result.scalar_one_or_none() is not None
                row = (
                    await session.execute(
                        text(
                            "SELECT content_hash FROM ingress_receipts "
                            "WHERE tenant_id = :tenant_id AND dedupe_key = :dedupe_key"
                        ),
                        values,
                    )
                ).one()
                if row.content_hash != values["content_hash"]:
                    raise IngressConflict("source identity conflict")
                await session.execute(
                    text(
                        "INSERT INTO ingress_replays "
                        "(tenant_id, replay_key, dedupe_key, content_hash, expires_at) "
                        "VALUES (:tenant_id, :replay_key, :dedupe_key, :content_hash, :expires_at) "
                        "ON CONFLICT (tenant_id, replay_key) DO NOTHING"
                    ),
                    values,
                )
                replay = (
                    await session.execute(
                        text(
                            "SELECT dedupe_key, content_hash FROM ingress_replays "
                            "WHERE tenant_id = :tenant_id AND replay_key = :replay_key"
                        ),
                        values,
                    )
                ).one()
                if (replay.dedupe_key, replay.content_hash) != (
                    values["dedupe_key"],
                    values["content_hash"],
                ):
                    raise IngressConflict("replay identity conflict")
                # A wait for another writer must not make old identity snapshots authoritative.
                if self._directory().resolve(binding.issuer, binding.subject) != binding:
                    raise AuthenticationError("ingress identity changed")
                if event.expires_at < datetime.now(UTC).timestamp():
                    raise AuthenticationError("ingress request expired")
                await self._acceptor.authorize(session, event)
                if created:
                    await self._acceptor.persist(session, event, str(values["receipt_ref"]))
        except SQLAlchemyError:
            raise IngressUnavailable("ingress storage unavailable") from None
