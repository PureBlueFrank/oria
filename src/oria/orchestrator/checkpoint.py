"""Tenant-isolated wrappers around LangGraph's official async SQL savers."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import SecretStr

from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import Context
from oria.storage.urls import psycopg_postgres_url

_TENANT_KEY = "oria_tenant_id"
_METADATA_TENANT_KEY = "oria_tenant_id"
_METADATA_THREAD_KEY = "oria_external_thread_id"

OfficialAsyncSaver = AsyncSqliteSaver | AsyncPostgresSaver


def checkpoint_config(ctx: Context) -> RunnableConfig:
    """Build an external LangGraph config without exposing the storage key."""

    return {
        "configurable": {
            "thread_id": ctx.thread_id,
            "checkpoint_ns": "",
            _TENANT_KEY: ctx.tenant_id,
        }
    }


def _storage_thread_id(tenant_id: str, external_thread_id: str) -> str:
    if not tenant_id or not external_thread_id:
        raise ValueError("tenant and external thread IDs must be non-empty")
    tenant = tenant_id.encode("utf-8")
    thread = external_thread_id.encode("utf-8")
    payload = (
        b"v1" + len(tenant).to_bytes(4, "big") + tenant + len(thread).to_bytes(4, "big") + thread
    )
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"oria_v1_{encoded}"


def _identity(config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ValueError("checkpoint config requires configurable values")
    tenant_id = configurable.get(_TENANT_KEY)
    thread_id = configurable.get("thread_id")
    if not isinstance(tenant_id, str) or not isinstance(thread_id, str):
        raise ValueError("checkpoint config requires tenant and external thread IDs")
    if not tenant_id or not thread_id:
        raise ValueError("checkpoint tenant and external thread IDs must be non-empty")
    return tenant_id, thread_id


def _qualify(config: RunnableConfig) -> RunnableConfig:
    tenant_id, thread_id = _identity(config)
    qualified = cast(RunnableConfig, dict(config))
    configurable = dict(config.get("configurable", {}))
    configurable["thread_id"] = _storage_thread_id(tenant_id, thread_id)
    configurable.pop(_TENANT_KEY, None)
    qualified["configurable"] = configurable
    return qualified


def _externalize(
    config: RunnableConfig | None,
    *,
    tenant_id: str,
    thread_id: str,
) -> RunnableConfig | None:
    if config is None:
        return None
    external = cast(RunnableConfig, dict(config))
    configurable = dict(config.get("configurable", {}))
    configurable["thread_id"] = thread_id
    configurable[_TENANT_KEY] = tenant_id
    external["configurable"] = configurable
    return external


class TenantCheckpointSaver(BaseCheckpointSaver[Any]):
    """Delegate official async checkpoint semantics behind a tenant-safe key."""

    def __init__(self, delegate: OfficialAsyncSaver) -> None:
        super().__init__(serde=delegate.serde)
        self._delegate = delegate

    async def setup(self) -> None:
        await self._delegate.setup()

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        tenant_id, thread_id = _identity(config)
        stored_metadata = cast(
            CheckpointMetadata,
            dict(metadata)
            | {
                _METADATA_TENANT_KEY: tenant_id,
                _METADATA_THREAD_KEY: thread_id,
            },
        )
        stored = await self._delegate.aput(
            _qualify(config), checkpoint, stored_metadata, new_versions
        )
        external = _externalize(stored, tenant_id=tenant_id, thread_id=thread_id)
        if external is None:
            raise AssertionError("checkpoint put returned no config")
        return external

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._delegate.aput_writes(_qualify(config), writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        tenant_id, thread_id = _identity(config)
        value = await self._delegate.aget_tuple(_qualify(config))
        if value is None:
            return None
        return self._externalize_tuple(value, tenant_id=tenant_id, thread_id=thread_id)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise ValueError("tenant-scoped checkpoint listing requires a config")
        tenant_id, thread_id = _identity(config)
        if before is not None and _identity(before) != (tenant_id, thread_id):
            raise ValueError("checkpoint before cursor belongs to another tenant or thread")
        qualified_before = _qualify(before) if before is not None else None
        async for value in self._delegate.alist(
            _qualify(config), filter=filter, before=qualified_before, limit=limit
        ):
            yield self._externalize_tuple(value, tenant_id=tenant_id, thread_id=thread_id)

    async def adelete_thread_for(self, *, tenant_id: str, thread_id: str) -> None:
        await self._delegate.adelete_thread(_storage_thread_id(tenant_id, thread_id))

    async def adelete_thread(self, thread_id: str) -> None:
        del thread_id
        raise ValueError("tenant ID is required; use adelete_thread_for")

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._delegate.get_next_version(current, channel)

    @staticmethod
    def _externalize_tuple(
        value: CheckpointTuple,
        *,
        tenant_id: str,
        thread_id: str,
    ) -> CheckpointTuple:
        metadata_tenant = value.metadata.get(_METADATA_TENANT_KEY)
        metadata_thread = value.metadata.get(_METADATA_THREAD_KEY)
        if metadata_tenant is not None and metadata_tenant != tenant_id:
            raise PermissionError("checkpoint metadata belongs to another tenant")
        if metadata_thread is not None and metadata_thread != thread_id:
            raise PermissionError("checkpoint metadata belongs to another thread")
        config = _externalize(value.config, tenant_id=tenant_id, thread_id=thread_id)
        if config is None:
            raise AssertionError("checkpoint tuple has no config")
        return CheckpointTuple(
            config=config,
            checkpoint=value.checkpoint,
            metadata=value.metadata,
            parent_config=_externalize(
                value.parent_config, tenant_id=tenant_id, thread_id=thread_id
            ),
            pending_writes=value.pending_writes,
        )


# Backward-compatible public name for callers that explicitly select SQLite.
TenantSqliteSaver = TenantCheckpointSaver


def _json_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=(
            ("oria.core.types", "NodeResult"),
            ("oria.core.types", "ResourceRef"),
        ),
    )


@asynccontextmanager
async def open_tenant_sqlite_saver(path: Path) -> AsyncIterator[TenantSqliteSaver]:
    """Open an official async SQLite saver with JSON-only serialization."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(path)
    try:
        delegate = AsyncSqliteSaver(connection, serde=_json_serializer())
        saver = TenantCheckpointSaver(delegate)
        await saver.setup()
        yield saver
    finally:
        await connection.close()


@asynccontextmanager
async def open_tenant_postgres_saver(
    url: SecretStr,
) -> AsyncIterator[TenantCheckpointSaver]:
    """Open the official PostgreSQL saver; its tables remain saver-owned."""

    async with AsyncPostgresSaver.from_conn_string(
        psycopg_postgres_url(url),
        serde=_json_serializer(),
    ) as delegate:
        saver = TenantCheckpointSaver(delegate)
        await saver.setup()
        yield saver


@asynccontextmanager
async def open_tenant_saver(
    config: ResolvedRuntimeConfig,
) -> AsyncIterator[TenantCheckpointSaver]:
    """Select the official saver matching the configured platform backend."""

    if config.storage.platform_db == "postgres":
        if config.storage.platform_url is None:
            raise ValueError("platform PostgreSQL URL is unavailable")
        async with open_tenant_postgres_saver(config.storage.platform_url) as saver:
            yield saver
        return
    async with open_tenant_sqlite_saver(config.data_paths.platform_db) as saver:
        yield saver
