"""Process-local session history with deterministic context compression."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from oria.core.context import Context
from oria.core.types import JsonValue, MemoryItem, Message

from .models import ContextBudget, FactLedger, FactLedgerEntry, FactValue, estimate_tokens

_SUMMARY_KIND = "oria_context_summary"
_FACT_KEY = re.compile(
    r"(?:^|_)(?:id|name|amount|revenue|gmv|count|total|rate|ratio|score|"
    r"status|result|conclusion|root_cause|reason|currency|date|time)$"
)
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|authorization|api_key|prompt|content)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?\d{10,15}$")
_MAX_FACT_STRING_LENGTH = 256


@dataclass(slots=True)
class _SessionRecord:
    messages: list[Message] = field(default_factory=list)
    ledger: FactLedger = field(default_factory=FactLedger)


def _namespace(ctx: Context) -> tuple[str, str]:
    return (ctx.tenant_id, ctx.session_id)


def _json_object(message: Message) -> dict[str, JsonValue] | None:
    if not isinstance(message.content, str) or not message.content:
        return None
    try:
        value = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_fact_value(value: JsonValue) -> FactValue | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str) or len(value) > _MAX_FACT_STRING_LENGTH:
        return None
    if _EMAIL.fullmatch(value) or _PHONE.fullmatch(value):
        return None
    return value


def _extract_from_value(
    value: JsonValue,
    *,
    role: Literal["assistant", "tool"],
    path: tuple[str, ...] = (),
) -> list[tuple[Literal["assistant", "tool"], str, FactValue]]:
    facts: list[tuple[Literal["assistant", "tool"], str, FactValue]] = []
    if isinstance(value, dict):
        for raw_key in sorted(value):
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                continue
            item = value[raw_key]
            child_path = (*path, key)
            safe_value = _safe_fact_value(item)
            if safe_value is not None and _FACT_KEY.search(key.lower()):
                facts.append((role, ".".join(child_path), safe_value))
            elif isinstance(item, (dict, list)):
                facts.extend(_extract_from_value(item, role=role, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            facts.extend(_extract_from_value(item, role=role, path=(*path, str(index))))
    return facts


def extract_facts(messages: Sequence[Message], ledger: FactLedger | None = None) -> FactLedger:
    """Extract allowlisted scalar facts from assistant/tool JSON in stable order."""

    accumulated = list(() if ledger is None else ledger.entries)
    known: set[tuple[object, ...]] = {
        (entry.source_role, entry.key, entry.value) for entry in accumulated
    }
    for message in messages:
        if message.role not in {"assistant", "tool"}:
            continue
        value = _json_object(message)
        if value is None or _SUMMARY_KIND in value:
            continue
        source_role = cast(Literal["assistant", "tool"], message.role)
        for role, key, fact_value in _extract_from_value(value, role=source_role):
            identity = (role, key, fact_value)
            if identity in known:
                continue
            known.add(identity)
            accumulated.append(
                FactLedgerEntry(
                    ordinal=len(accumulated),
                    source_role=role,
                    key=key,
                    value=fact_value,
                )
            )
    return FactLedger(entries=tuple(accumulated))


def _summary_message(entries: Sequence[FactLedgerEntry]) -> Message:
    payload = {
        _SUMMARY_KIND: {
            "version": 1,
            "facts": [{"key": entry.key, "value": entry.value} for entry in entries],
        }
    }
    return Message(
        role="assistant",
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _estimate(messages: Sequence[Message], budget: ContextBudget) -> int:
    return estimate_tokens(
        messages,
        chars_per_token=budget.chars_per_token,
        message_overhead_tokens=budget.message_overhead_tokens,
    )


def compress_history(
    messages: Sequence[Message],
    budget: ContextBudget,
    ledger: FactLedger | None = None,
) -> tuple[list[Message], FactLedger, bool]:
    """Compress old non-system messages and return history, ledger, changed flag."""

    current = list(messages)
    if _estimate(current, budget) <= budget.message_token_limit:
        return current, FactLedger() if ledger is None else ledger, False

    system_messages = [message for message in current if message.role == "system"]
    non_system = [message for message in current if message.role != "system"]
    updated_ledger = extract_facts(non_system, ledger)

    included_facts = list(updated_ledger.entries)
    while (
        included_facts
        and _estimate([*system_messages, _summary_message(included_facts)], budget)
        > budget.message_token_limit
    ):
        included_facts.pop(0)
    summary = _summary_message(included_facts)

    compressed: list[Message] = [*system_messages, summary]
    for message in reversed(non_system):
        candidate = [*compressed, message]
        if _estimate(candidate, budget) > budget.message_token_limit:
            break
        compressed.append(message)
    if len(compressed) > len(system_messages) + 1:
        tail = compressed[len(system_messages) + 1 :]
        compressed = [*system_messages, summary, *reversed(tail)]
    return compressed, updated_ledger, True


class InMemoryMemory:
    """Community session memory, isolated by tenant and session within one process."""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()
        self._records: dict[tuple[str, str], _SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def load(self, ctx: Context) -> list[Message]:
        async with self._lock:
            record = self._records.get(_namespace(ctx))
            return [] if record is None else list(record.messages)

    async def append(self, msg: Message, ctx: Context) -> None:
        async with self._lock:
            record = self._records.setdefault(_namespace(ctx), _SessionRecord())
            record.messages.append(msg)

    async def replace(
        self,
        messages: Sequence[Message],
        ctx: Context,
        *,
        ledger: FactLedger | None = None,
    ) -> None:
        """Synchronize checkpoint-backed history before applying context governance."""

        async with self._lock:
            record = self._records.setdefault(_namespace(ctx), _SessionRecord())
            record.messages = list(messages)
            if ledger is not None:
                record.ledger = ledger

    async def search(self, query: str, ctx: Context, k: int = 5) -> list[MemoryItem]:
        """Return no long-term memories; opt-in vector search belongs to V0.5-T02."""

        del query, ctx, k
        return []

    async def compress(self, ctx: Context) -> None:
        async with self._lock:
            record = self._records.setdefault(_namespace(ctx), _SessionRecord())
            messages, ledger, _ = compress_history(
                record.messages,
                self.budget,
                record.ledger,
            )
            record.messages = messages
            record.ledger = ledger

    async def fact_ledger(self, ctx: Context) -> FactLedger:
        async with self._lock:
            record = self._records.get(_namespace(ctx))
            return FactLedger() if record is None else record.ledger
