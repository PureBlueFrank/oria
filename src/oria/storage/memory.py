"""SQLite repository for namespaced long-term memories and embeddings."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.context import Context
from oria.core.types import MemoryItem, PolicyDecision


class MemoryRepositoryError(RuntimeError):
    """Safe persistence failure without memory content or SQL details."""


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    item: MemoryItem
    embedding: tuple[float, ...]


class SQLiteMemoryRepository:
    """Store memory bodies and vector projections in one platform transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(
        self,
        item: MemoryItem,
        embedding: list[float],
        *,
        projection_id: str,
        now: datetime,
    ) -> None:
        content_hash = _content_hash(item.content)
        values = item.model_dump(mode="json") | {
            "memory_id": item.id,
            "embedding_json": json.dumps(embedding, separators=(",", ":")),
            "projection_id": projection_id,
            "content_hash": content_hash,
            "created_at": now,
            "updated_at": now,
        }
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO memory_items (tenant_id, subject_id, memory_id, content, "
                        "provenance, confidence, sensitivity, expires_at, score, content_hash, "
                        "created_at, updated_at, deleted_at) VALUES (:tenant_id, :subject_id, "
                        ":memory_id, :content, :provenance, :confidence, :sensitivity, "
                        ":expires_at, :score, :content_hash, :created_at, :updated_at, NULL)"
                    ),
                    values,
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_embeddings (tenant_id, subject_id, memory_id, "
                        "projection_id, embedding_json, created_at, updated_at) VALUES "
                        "(:tenant_id, :subject_id, :memory_id, :projection_id, :embedding_json, "
                        ":created_at, :updated_at)"
                    ),
                    values,
                )
        except IntegrityError as exc:
            raise ValueError("memory already exists") from exc
        except SQLAlchemyError as exc:
            raise MemoryRepositoryError("memory persistence failed") from exc

    async def list_active(
        self,
        ctx: Context,
        *,
        now: datetime,
        minimum_confidence: float | None = None,
        sensitivities: frozenset[str] | None = None,
        projection_id: str | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        clauses = [
            "m.tenant_id = :tenant_id",
            "m.subject_id = :subject_id",
            "m.deleted_at IS NULL",
            "(m.expires_at IS NULL OR m.expires_at > :now)",
        ]
        values: dict[str, Any] = {
            "tenant_id": ctx.tenant_id,
            "subject_id": ctx.actor.subject_id,
            "now": now,
        }
        join = ""
        columns = (
            "m.memory_id, m.tenant_id, m.subject_id, m.content, m.provenance, "
            "m.confidence, m.sensitivity, m.expires_at, m.score"
        )
        if projection_id is not None:
            join = (
                " JOIN memory_embeddings e ON e.tenant_id = m.tenant_id AND "
                "e.subject_id = m.subject_id AND e.memory_id = m.memory_id"
            )
            columns += ", e.embedding_json"
            clauses.append("e.projection_id = :projection_id")
            values["projection_id"] = projection_id
        if minimum_confidence is not None:
            clauses.append("m.confidence >= :minimum_confidence")
            values["minimum_confidence"] = minimum_confidence
        if sensitivities is not None:
            names: list[str] = []
            for index, sensitivity in enumerate(sorted(sensitivities)):
                name = f"sensitivity_{index}"
                names.append(f":{name}")
                values[name] = sensitivity
            clauses.append(f"m.sensitivity IN ({', '.join(names)})")
        query = (
            f"SELECT {columns} FROM memory_items m{join} WHERE "
            + " AND ".join(clauses)
            + " ORDER BY m.created_at, m.memory_id"
        )
        try:
            async with self._sessions() as session:
                result = await session.execute(text(query), values)
                rows = result.mappings().all()
        except SQLAlchemyError as exc:
            raise MemoryRepositoryError("memory read failed") from exc
        candidates: list[MemoryCandidate] = []
        for row in rows:
            try:
                embedding = (
                    tuple(float(value) for value in json.loads(str(row["embedding_json"])))
                    if projection_id is not None
                    else ()
                )
                candidates.append(MemoryCandidate(item=_item_from_row(row), embedding=embedding))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MemoryRepositoryError("memory projection is invalid") from exc
        return tuple(candidates)

    async def delete(
        self,
        memory_id: str,
        ctx: Context,
        *,
        now: datetime,
        decision: PolicyDecision,
    ) -> bool:
        values = {
            "tenant_id": ctx.tenant_id,
            "subject_id": ctx.actor.subject_id,
            "memory_id": memory_id,
            "updated_at": now,
        }
        try:
            async with self._sessions.begin() as session:
                result = await session.execute(
                    text(
                        "SELECT content_hash FROM memory_items WHERE tenant_id = :tenant_id "
                        "AND subject_id = :subject_id AND memory_id = :memory_id "
                        "AND deleted_at IS NULL"
                    ),
                    values,
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return False
                await session.execute(
                    text(
                        "DELETE FROM memory_embeddings WHERE tenant_id = :tenant_id "
                        "AND subject_id = :subject_id AND memory_id = :memory_id"
                    ),
                    values,
                )
                await session.execute(
                    text(
                        "UPDATE memory_items SET content = '', provenance = 'deleted', "
                        "confidence = 0, sensitivity = 'deleted', score = 0, "
                        "updated_at = :updated_at, deleted_at = :updated_at WHERE "
                        "tenant_id = :tenant_id AND subject_id = :subject_id "
                        "AND memory_id = :memory_id"
                    ),
                    values,
                )
                await self._append_delete_audit(
                    session,
                    ctx,
                    memory_id=memory_id,
                    object_hash=str(row["content_hash"]),
                    now=now,
                    decision=decision,
                )
        except SQLAlchemyError as exc:
            raise MemoryRepositoryError("memory deletion failed") from exc
        return True

    @staticmethod
    async def _append_delete_audit(
        session: AsyncSession,
        ctx: Context,
        *,
        memory_id: str,
        object_hash: str,
        now: datetime,
        decision: PolicyDecision,
    ) -> None:
        args = f"{ctx.tenant_id}:{ctx.actor.subject_id}:{memory_id}"
        args_hash = "sha256:" + hashlib.sha256(args.encode()).hexdigest()
        await session.execute(
            text(
                "INSERT INTO audit_events (event_id, occurred_at, tenant_id, actor, action, "
                "resource_type, resource_id, resource_tenant_id, decision, policy_version, "
                "args_hash, result, correlation_id, payload_json) VALUES (:event_id, "
                ":occurred_at, :tenant_id, :actor, 'memory:deleted', 'memory', :memory_id, "
                ":tenant_id, 'allow', :policy_version, :args_hash, 'success', "
                ":correlation_id, :payload_json)"
            ),
            {
                "event_id": f"aud_{uuid.uuid4().hex}",
                "occurred_at": now,
                "tenant_id": ctx.tenant_id,
                "actor": ctx.actor.subject_id,
                "memory_id": memory_id,
                "policy_version": decision.policy_version,
                "args_hash": args_hash,
                "correlation_id": ctx.correlation_id,
                "payload_json": json.dumps(
                    {"object_hash": object_hash}, sort_keys=True, separators=(",", ":")
                ),
            },
        )


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def _item_from_row(row: Any) -> MemoryItem:
    return MemoryItem(
        id=str(row["memory_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        content=str(row["content"]),
        provenance=str(row["provenance"]),
        confidence=float(row["confidence"]),
        sensitivity=str(row["sensitivity"]),
        expires_at=row["expires_at"],
        score=float(row["score"]),
    )
