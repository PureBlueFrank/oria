"""V0.6-T01 complete async checkpoint delegation contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    empty_checkpoint,
)

from oria.orchestrator.checkpoint import TenantCheckpointSaver

pytestmark = [pytest.mark.contract, pytest.mark.security]


class _RecordingSaver(BaseCheckpointSaver[str]):
    def __init__(self) -> None:
        super().__init__()
        self.put_config: RunnableConfig | None = None
        self.write_config: RunnableConfig | None = None
        self.writes: Sequence[tuple[str, Any]] = ()
        self.task_path = ""
        self.value: CheckpointTuple | None = None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self.put_config = config
        stored = {
            "configurable": dict(config["configurable"]) | {"checkpoint_id": checkpoint["id"]}
        }
        self.value = CheckpointTuple(
            config=stored,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config={
                "configurable": dict(config["configurable"]) | {"checkpoint_id": "parent"}
            },
            pending_writes=(("task", "channel", "pending"),),
        )
        return stored

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.write_config = config
        self.writes = writes
        self.task_path = task_path

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.value

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        del config, filter, before, limit
        if self.value is not None:
            yield self.value

    async def adelete_thread(self, thread_id: str) -> None:
        del thread_id


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "external-thread",
            "checkpoint_ns": "agent:research",
            "oria_tenant_id": "tenant-a",
        }
    }


@pytest.mark.asyncio
async def test_adapter_delegates_full_async_contract_without_losing_saver_metadata() -> None:
    delegate = _RecordingSaver()
    saver = TenantCheckpointSaver(delegate)
    checkpoint = empty_checkpoint()
    checkpoint["channel_versions"] = {"messages": "7"}
    checkpoint["versions_seen"] = {"node": {"messages": "6"}}
    metadata: CheckpointMetadata = {"source": "loop", "step": 4, "parents": {}}

    saved = await saver.aput(_config(), checkpoint, metadata, {"messages": "7"})
    await saver.aput_writes(
        saved,
        (("messages", {"safe": "value"}),),
        "task-1",
        "push:agent",
    )
    loaded = await saver.aget_tuple(saved)
    listed = [item async for item in saver.alist(_config(), before=saved, limit=1)]

    assert loaded is not None and listed == [loaded]
    assert loaded.checkpoint is checkpoint
    assert loaded.metadata["source"] == metadata["source"]
    assert loaded.metadata["step"] == metadata["step"]
    assert loaded.metadata["parents"] == metadata["parents"]
    assert loaded.metadata["oria_tenant_id"] == "tenant-a"
    assert loaded.metadata["oria_external_thread_id"] == "external-thread"
    assert loaded.pending_writes == (("task", "channel", "pending"),)
    assert loaded.parent_config is not None
    assert loaded.parent_config["configurable"]["thread_id"] == "external-thread"
    assert saved["configurable"]["checkpoint_ns"] == "agent:research"
    assert delegate.put_config is not None and delegate.write_config is not None
    stored_thread = delegate.put_config["configurable"]["thread_id"]
    assert stored_thread.startswith("oria_v1_")
    assert delegate.write_config["configurable"]["thread_id"] == stored_thread
    assert delegate.task_path == "push:agent"
    assert delegate.writes == (("messages", {"safe": "value"}),)
    assert "oria_v1_" not in repr((saved, loaded, listed))
