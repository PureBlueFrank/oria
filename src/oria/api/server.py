"""Explicit local or JWT server assembly, without ambient credential loading."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from oria.api.app import create_app
from oria.api.approvals import ApprovalAPI
from oria.api.identity import (
    IdentityBinding,
    IdentityDirectory,
    JWTAuthenticator,
    JWTSettings,
    file_directory,
)
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ValueModel
from oria.ingress.feishu import FeishuIngress, FeishuSettings
from oria.migrations.runner import validate_sqlite_databases
from oria.permission.local import local_cli_executor, local_operator
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository


class ServerSettings(ValueModel):
    jwt: JWTSettings
    identity_file: Path
    feishu: FeishuSettings | None = None
    approval_runtime_config: Path | None = None


@asynccontextmanager
async def approval_runtime(
    path: Path,
    directory: Callable[[], IdentityDirectory],
) -> AsyncIterator[ApprovalAPI]:
    # Explicit file and empty environment prevent ambient provider/credential activation.
    try:
        config = resolve_runtime_config(config_path=path, environ={}, cwd=path.parent)
    except (ValueError, OSError):
        raise ValueError("invalid approval runtime configuration") from None
    if (
        config.llm.provider != "mock"
        or config.embedding.provider != "fixture"
        or config.storage.platform_db != "sqlite"
        or config.storage.biz_db != "sqlite"
    ):
        raise ValueError("approval server currently requires Mock/Fixture and SQLite")
    validate_sqlite_databases(config)
    identities = directory().bindings
    async with DatabaseResources(config) as databases:
        runtime = await build_runtime(
            config,
            resource_factories=(lambda: databases,),
            trusted_actors=tuple(item.actor for item in identities),
            trusted_executors=tuple(item.executor for item in identities),
        )
        async with runtime:
            yield ApprovalAPI(
                runtime, SQLiteApprovalRepository(databases.platform_sessions), directory
            )


def build_api(*, config: Path | None, local: bool, host: str) -> FastAPI:
    if local:
        if config is not None or host not in {"127.0.0.1", "::1"}:
            raise ValueError("local mode requires loopback and no server configuration")
        return create_app(
            local_identity=IdentityBinding(
                issuer="local",
                subject="operator",
                actor=local_operator(),
                executor=local_cli_executor().model_copy(update={"subject_id": "oria-api"}),
            )
        )
    if config is None:
        raise ValueError("provide server configuration or explicitly select --local")
    try:
        settings = ServerSettings.model_validate_json(config.read_bytes())
        directory_path = settings.identity_file
        if not directory_path.is_absolute():
            directory_path = config.parent / directory_path
        directory = file_directory(directory_path)
        directory()  # Validate at startup; all later boundaries reload it.
    except (ValueError, OSError, PermissionError):
        raise ValueError("invalid server configuration or identity directory") from None
    runtime_path = settings.approval_runtime_config
    if runtime_path is not None and not runtime_path.is_absolute():
        runtime_path = config.parent / runtime_path
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    return create_app(
        authenticator=JWTAuthenticator(settings.jwt, directory, client),
        feishu=FeishuIngress(settings.feishu, directory) if settings.feishu else None,
        close=client.aclose,
        approval_factory=(lambda: approval_runtime(runtime_path, directory))
        if runtime_path is not None
        else None,
    )
