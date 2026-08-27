"""Process-scoped SQLAlchemy engines and async session factories."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from oria.config.models import ResolvedRuntimeConfig


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _create_sqlite_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(_sqlite_url(path), pool_pre_ping=True)
    event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return engine


class DatabaseResources:
    """The only process resource used to create local SQL repositories."""

    __slots__ = (
        "_business_engine",
        "_platform_engine",
        "business_sessions",
        "platform_sessions",
    )

    def __init__(self, config: ResolvedRuntimeConfig) -> None:
        paths = config.data_paths
        self._platform_engine = _create_sqlite_engine(paths.platform_db)
        self._business_engine = _create_sqlite_engine(paths.business_db)
        self.platform_sessions = async_sessionmaker(
            self._platform_engine, class_=AsyncSession, expire_on_commit=False
        )
        self.business_sessions = async_sessionmaker(
            self._business_engine, class_=AsyncSession, expire_on_commit=False
        )

    async def __aenter__(self) -> DatabaseResources:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._business_engine.dispose()
        await self._platform_engine.dispose()
