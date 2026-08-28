"""Official SQLite checkpoint adapter isolation contracts."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph

from oria.orchestrator.checkpoint import open_tenant_sqlite_saver

pytestmark = [pytest.mark.contract, pytest.mark.security]


class _CounterState(TypedDict):
    value: int


def _config(tenant_id: str, thread_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "oria_tenant_id": tenant_id,
        }
    }


@pytest.mark.asyncio
async def test_same_external_thread_is_isolated_and_storage_key_never_leaks(
    tmp_path: Path,
) -> None:
    async with open_tenant_sqlite_saver(tmp_path / "checkpoints.sqlite3") as saver:
        tenant_a = _config("tenant-a", "shared-thread")
        tenant_b = _config("tenant-b", "shared-thread")
        saved_a = await saver.aput(
            tenant_a,
            empty_checkpoint(),
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        saved_b = await saver.aput(
            tenant_b,
            empty_checkpoint(),
            {"source": "input", "step": 0, "parents": {}},
            {},
        )

        tuple_a = await saver.aget_tuple(tenant_a)
        tuple_b = await saver.aget_tuple(tenant_b)
        assert tuple_a is not None and tuple_b is not None
        assert tuple_a.checkpoint["id"] != tuple_b.checkpoint["id"]
        assert tuple_a.config["configurable"]["thread_id"] == "shared-thread"
        assert tuple_b.config["configurable"]["thread_id"] == "shared-thread"
        assert "oria_v1_" not in repr((saved_a, saved_b, tuple_a, tuple_b))

        listed_a = [item async for item in saver.alist(tenant_a)]
        listed_b = [item async for item in saver.alist(tenant_b)]
        assert [item.checkpoint["id"] for item in listed_a] == [tuple_a.checkpoint["id"]]
        assert [item.checkpoint["id"] for item in listed_b] == [tuple_b.checkpoint["id"]]

        await saver.adelete_thread_for(tenant_id="tenant-a", thread_id="shared-thread")
        assert await saver.aget_tuple(tenant_a) is None
        assert await saver.aget_tuple(tenant_b) is not None


@pytest.mark.asyncio
async def test_checkpoint_listing_requires_tenant_scope(tmp_path: Path) -> None:
    async with open_tenant_sqlite_saver(tmp_path / "checkpoints.sqlite3") as saver:
        with pytest.raises(ValueError, match="tenant-scoped"):
            _ = [item async for item in saver.alist(None)]
        with pytest.raises(ValueError, match="tenant ID"):
            await saver.adelete_thread("ambiguous-thread")


@pytest.mark.asyncio
async def test_official_state_graph_can_persist_and_resume_through_adapter(tmp_path: Path) -> None:
    async with open_tenant_sqlite_saver(tmp_path / "graph.sqlite3") as saver:
        builder = StateGraph(_CounterState)
        builder.add_node("increment", lambda state: {"value": state["value"] + 1})
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        graph = builder.compile(checkpointer=saver)
        tenant_a = _config("tenant-a", "shared-thread")
        tenant_b = _config("tenant-b", "shared-thread")

        assert (await graph.ainvoke({"value": 1}, config=tenant_a))["value"] == 2
        assert (await graph.ainvoke({"value": 40}, config=tenant_b))["value"] == 41
        state_a = await graph.aget_state(tenant_a)
        state_b = await graph.aget_state(tenant_b)

        assert state_a.values == {"value": 2}
        assert state_b.values == {"value": 41}
        assert "oria_v1_" not in repr((state_a.config, state_b.config))
