"""V0.5-T02 contracts for opt-in, namespaced long-term memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import MemoryItem, Principal
from oria.data import initialize_data
from oria.memory import PersistentMemory

pytestmark = pytest.mark.contract


def _principal(subject_id: str, tenant_id: str, *, service: bool = False) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        kind="service" if service else "human",
        roles=("runtime",) if service else ("operator",),
        authn_method="test-trusted",
    )


async def _runtime(tmp_path: Path, clock):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    actors = (
        _principal("subject-a", "tenant-a"),
        _principal("subject-b", "tenant-a"),
        _principal("subject-a", "tenant-b"),
    )
    executors = (
        _principal("executor-a", "tenant-a", service=True),
        _principal("executor-b", "tenant-b", service=True),
    )
    runtime = await build_runtime(
        config,
        clock=clock,
        trusted_actors=actors,
        trusted_executors=executors,
    )
    contexts = {
        "a": runtime.new_context(
            actor=actors[0],
            executor=executors[0],
            session_id="session-a",
            thread_id="thread-a",
            run_id="run-a",
        ),
        "subject_b": runtime.new_context(
            actor=actors[1],
            executor=executors[0],
            session_id="session-b",
            thread_id="thread-b",
            run_id="run-b",
        ),
        "tenant_b": runtime.new_context(
            actor=actors[2],
            executor=executors[1],
            session_id="session-c",
            thread_id="thread-c",
            run_id="run-c",
        ),
    }
    return config, runtime, contexts, actors, executors


@pytest.mark.asyncio
async def test_opt_in_memory_survives_sessions_and_supports_export_delete(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    config, runtime, contexts, actors, executors = await _runtime(tmp_path, lambda: now)
    try:
        memory = runtime.memory
        assert isinstance(memory, PersistentMemory)
        saved = await memory.save_fact(
            "用户偏好低糖饮品; email=person@example.com",
            contexts["a"],
            provenance="user-explicit",
            confidence=0.95,
            sensitivity="low",
            expires_at=now + timedelta(days=30),
            opt_in=True,
        )
        with pytest.raises(PermissionError, match="opt-in"):
            await memory.save_fact(
                "不得后台自动保存",
                contexts["a"],
                provenance="background-summary",
                confidence=0.9,
                sensitivity="low",
                opt_in=False,
            )
    finally:
        await runtime.aclose()

    second = await build_runtime(
        config,
        clock=lambda: now,
        trusted_actors=actors,
        trusted_executors=executors,
    )
    try:
        new_session = second.new_context(
            actor=actors[0],
            executor=executors[0],
            session_id="new-session",
            thread_id="new-thread",
            run_id="new-run",
        )
        memory = second.memory
        assert isinstance(memory, PersistentMemory)
        found = await memory.search("低糖", new_session)
        assert [item.id for item in found] == [saved.id]
        assert found[0].content == "用户偏好低糖饮品; email=[REDACTED]"
        exported = await memory.export(new_session)
        assert exported == [
            {
                "id": saved.id,
                "content": "用户偏好低糖饮品; email=[REDACTED]",
                "provenance": "user-explicit",
                "confidence": 0.95,
                "sensitivity": "low",
                "expires_at": "2026-10-10T00:00:00Z",
                "score": 0.0,
            }
        ]
        assert await memory.delete(saved.id, new_session) is True
        assert await memory.search("低糖", new_session) == []
        assert await memory.view(new_session) == []
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_namespace_ttl_and_confidence_filtering(tmp_path: Path) -> None:
    current = [datetime(2026, 9, 10, tzinfo=UTC)]
    _, runtime, contexts, _, _ = await _runtime(tmp_path, lambda: current[0])
    try:
        memory = runtime.memory
        assert isinstance(memory, PersistentMemory)
        visible = await memory.save_fact(
            "当前用户喜欢乌龙茶",
            contexts["a"],
            provenance="user-explicit",
            confidence=0.9,
            sensitivity="low",
            expires_at=current[0] + timedelta(hours=1),
            opt_in=True,
        )
        low_confidence = await memory.save_fact(
            "模型猜测用户喜欢极甜饮品",
            contexts["a"],
            provenance="model-inference",
            confidence=0.2,
            sensitivity="low",
            opt_in=True,
        )
        assert [item.id for item in await memory.search("饮品", contexts["a"])] == [visible.id]
        assert {item.id for item in await memory.view(contexts["a"])} == {
            visible.id,
            low_confidence.id,
        }
        assert await memory.search("饮品", contexts["subject_b"]) == []
        assert await memory.search("饮品", contexts["tenant_b"]) == []

        wrong_namespace = MemoryItem(
            id="mem_cross_tenant",
            tenant_id="tenant-b",
            subject_id="subject-a",
            content="cross tenant",
            provenance="user-explicit",
            confidence=0.9,
            sensitivity="low",
            score=0.0,
        )
        with pytest.raises(PermissionError, match="not authorized"):
            await memory.save(wrong_namespace, contexts["a"], opt_in=True)

        current[0] += timedelta(hours=2)
        assert await memory.search("乌龙茶", contexts["a"]) == []
        assert [item.id for item in await memory.view(contexts["a"])] == [low_confidence.id]
    finally:
        await runtime.aclose()
