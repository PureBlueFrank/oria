"""Process-scoped SQLAlchemy engines and async session factories."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from oria.config.models import ResolvedRuntimeConfig


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


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
        self._platform_engine: AsyncEngine = create_async_engine(
            _sqlite_url(paths.platform_db), pool_pre_ping=True
        )
        self._business_engine: AsyncEngine = create_async_engine(
            _sqlite_url(paths.business_db), pool_pre_ping=True
        )
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
