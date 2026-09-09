"""Memory Protocol contract tests for the V0.5 community implementation."""

from __future__ import annotations

import json
from typing import cast

import pytest

from oria.core.context import Context, RuntimeServices
from oria.core.protocols import Memory
from oria.core.types import Message, Principal
from oria.memory import ContextBudget, InMemoryMemory, estimate_tokens

pytestmark = pytest.mark.contract


def _context(tenant_id: str, session_id: str) -> Context:
    actor = Principal(
        subject_id=f"actor-{tenant_id}",
        tenant_id=tenant_id,
        kind="human",
        roles=("operator",),
        authn_method="test",
    )
    executor = Principal(
        subject_id=f"executor-{tenant_id}",
        tenant_id=tenant_id,
        kind="service",
        roles=("runtime",),
        authn_method="test",
    )
    return Context(
        runtime=cast(RuntimeServices, object()),
        actor=actor,
        executor=executor,
        session_id=session_id,
        thread_id=f"thread-{session_id}",
        run_id=f"run-{session_id}",
        correlation_id=f"correlation-{session_id}",
    )


def _accepts_memory_protocol(memory: Memory) -> Memory:
    return memory


@pytest.mark.asyncio
async def test_append_load_and_search_stub_follow_memory_protocol() -> None:
    memory = _accepts_memory_protocol(InMemoryMemory())
    ctx = _context("tenant-a", "session-a")
    message = Message(role="user", content="hello")

    await memory.append(message, ctx)

    assert await memory.load(ctx) == [message]
    assert await memory.search("anything", ctx, k=3) == []


@pytest.mark.asyncio
async def test_short_history_is_isolated_by_tenant_and_session() -> None:
    memory = InMemoryMemory()
    ctx_a = _context("tenant-a", "session-shared")
    ctx_b = _context("tenant-b", "session-shared")
    ctx_c = _context("tenant-a", "session-other")

    await memory.append(Message(role="user", content="tenant-a"), ctx_a)
    await memory.append(Message(role="user", content="tenant-b"), ctx_b)

    assert [message.content for message in await memory.load(ctx_a)] == ["tenant-a"]
    assert [message.content for message in await memory.load(ctx_b)] == ["tenant-b"]
    assert await memory.load(ctx_c) == []


@pytest.mark.asyncio
async def test_compress_retains_serializable_ledger_and_obeys_budget() -> None:
    budget = ContextBudget(max_context_tokens=220, reserve_tokens=20)
    memory = InMemoryMemory(budget)
    ctx = _context("tenant-a", "session-a")
    await memory.append(Message(role="system", content="system rules"), ctx)
    await memory.append(
        Message(
            role="tool",
            tool_call_id="fact-call",
            content=json.dumps({"merchant_id": "m-100", "amount": 9900, "details": "x" * 800}),
        ),
        ctx,
    )
    await memory.append(Message(role="user", content="latest question"), ctx)

    await memory.compress(ctx)

    loaded = await memory.load(ctx)
    ledger = await memory.fact_ledger(ctx)
    serialized = ledger.model_dump(mode="json")
    assert json.loads(json.dumps(serialized)) == serialized
    assert {entry.key: entry.value for entry in ledger.entries} == {
        "amount": 9900,
        "merchant_id": "m-100",
    }
    assert estimate_tokens(loaded) <= budget.message_token_limit
