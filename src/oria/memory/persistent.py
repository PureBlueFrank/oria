"""Policy-authorized, opt-in long-term memory layered over session history."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from oria.core.context import Context
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    MemoryItem,
    PolicyDecision,
    ResourceRef,
)
from oria.storage.memory import SQLiteMemoryRepository

from .models import ContextBudget
from .store import InMemoryMemory

MINIMUM_INJECTION_CONFIDENCE = 0.7
LOW_SENSITIVITY = frozenset({"low", "public", "internal"})
MAX_MEMORY_CONTENT_LENGTH = 4000
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\d)\+?\d[\d -]{8,}\d(?!\d)")
_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer|password|secret|token)\b\s*[:=]\s*\S+"
)
_REDACTED = "[REDACTED]"


class PersistentMemory(InMemoryMemory):
    """Keep T01 session state in process and T02 opt-in memories in SQLite."""

    def __init__(
        self,
        budget: ContextBudget,
        repository: SQLiteMemoryRepository,
        *,
        projection_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(budget)
        self._repository = repository
        self._projection_id = projection_id
        self._clock = clock
        self._search_cache: dict[tuple[str, str, str, int], tuple[MemoryItem, ...]] = {}

    async def save(self, item: MemoryItem, ctx: Context, *, opt_in: bool) -> MemoryItem:
        """Persist a deliberately requested memory after namespace and content checks."""

        await self._authorize("memory:write", item.id, item.tenant_id, ctx)
        if not opt_in:
            raise PermissionError("long-term memory requires explicit opt-in")
        if item.tenant_id != ctx.tenant_id or item.subject_id != ctx.actor.subject_id:
            raise PermissionError("memory namespace is not authorized")
        if item.expires_at is not None:
            if item.expires_at.tzinfo is None or item.expires_at.utcoffset() is None:
                raise ValueError("memory expiry must include a timezone")
            if item.expires_at <= self._clock():
                raise ValueError("memory expiry must be in the future")
        if item.sensitivity not in LOW_SENSITIVITY:
            raise ValueError("sensitive content cannot be saved to long-term memory")
        content = redact_memory_content(item.content)
        if not content or len(content) > MAX_MEMORY_CONTENT_LENGTH:
            raise ValueError("memory content length is invalid")
        embedder = ctx.embedder
        if embedder is None:
            raise RuntimeError("memory embedding service is unavailable")
        vectors = await embedder.embed([content], ctx)
        if len(vectors) != 1 or len(vectors[0]) != embedder.dim:
            raise ValueError("memory embedding has an invalid dimension")
        stored = item.model_copy(update={"content": content, "score": 0.0})
        await self._repository.add(
            stored,
            vectors[0],
            projection_id=self._projection_id,
            now=self._clock(),
        )
        self._invalidate(ctx)
        return stored

    async def save_fact(
        self,
        content: str,
        ctx: Context,
        *,
        provenance: str,
        confidence: float,
        sensitivity: str,
        expires_at: datetime | None = None,
        opt_in: bool,
    ) -> MemoryItem:
        item = MemoryItem(
            id=f"mem_{uuid.uuid4().hex}",
            tenant_id=ctx.tenant_id,
            subject_id=ctx.actor.subject_id,
            content=content,
            provenance=provenance,
            confidence=confidence,
            sensitivity=sensitivity,
            expires_at=expires_at,
            score=0.0,
        )
        return await self.save(item, ctx, opt_in=opt_in)

    async def search(self, query: str, ctx: Context, k: int = 5) -> list[MemoryItem]:
        await self._authorize("memory:read", "namespace", ctx.tenant_id, ctx)
        if not query.strip():
            raise ValueError("memory search query must be non-empty")
        if k <= 0:
            raise ValueError("memory search limit must be positive")
        key = (ctx.tenant_id, ctx.actor.subject_id, query, k)
        cached = self._search_cache.get(key)
        if cached is not None:
            return list(cached)
        embedder = ctx.embedder
        if embedder is None:
            raise RuntimeError("memory embedding service is unavailable")
        vectors = await embedder.embed([query], ctx)
        if len(vectors) != 1:
            raise ValueError("memory query embedding is invalid")
        candidates = await self._repository.list_active(
            ctx,
            now=self._clock(),
            minimum_confidence=MINIMUM_INJECTION_CONFIDENCE,
            sensitivities=LOW_SENSITIVITY,
            projection_id=self._projection_id,
        )
        scored = [
            candidate.item.model_copy(
                update={"score": _cosine_similarity(vectors[0], candidate.embedding)}
            )
            for candidate in candidates
        ]
        result = tuple(sorted(scored, key=lambda value: (-value.score, value.id))[:k])
        self._search_cache[key] = result
        return list(result)

    async def view(self, ctx: Context) -> list[MemoryItem]:
        await self._authorize("memory:read", "namespace", ctx.tenant_id, ctx)
        candidates = await self._repository.list_active(ctx, now=self._clock())
        return [candidate.item for candidate in candidates]

    async def delete(self, memory_id: str, ctx: Context) -> bool:
        decision = await self._authorize("memory:delete", memory_id, ctx.tenant_id, ctx)
        deleted = await self._repository.delete(
            memory_id,
            ctx,
            now=self._clock(),
            decision=decision,
        )
        self._invalidate(ctx)
        return deleted

    async def export(self, ctx: Context) -> list[dict[str, object]]:
        await self._authorize("memory:export", "namespace", ctx.tenant_id, ctx)
        candidates = await self._repository.list_active(ctx, now=self._clock())
        return [
            candidate.item.model_dump(
                mode="json",
                exclude={"tenant_id", "subject_id"},
            )
            for candidate in candidates
        ]

    async def _authorize(
        self,
        action: str,
        resource_id: str,
        tenant_id: str,
        ctx: Context,
    ) -> PolicyDecision:
        decision = await ctx.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=action,
                resource=ResourceRef(
                    resource_type="memory",
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            ),
            ctx,
        )
        if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
            raise PermissionError("memory operation is not authorized")
        return decision

    def _invalidate(self, ctx: Context) -> None:
        namespace = (ctx.tenant_id, ctx.actor.subject_id)
        self._search_cache = {
            key: value for key, value in self._search_cache.items() if key[:2] != namespace
        }


def redact_memory_content(content: str) -> str:
    """Remove common credential and direct-identifier patterns before persistence."""

    redacted = _SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", content.strip())
    redacted = _EMAIL.sub(_REDACTED, redacted)
    return _PHONE.sub(_REDACTED, redacted)


def _cosine_similarity(left: list[float], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("memory embedding dimension mismatch")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def memory_content_hash(content: str) -> str:
    """Return the non-reversible object hash used by lifecycle evidence."""

    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()
