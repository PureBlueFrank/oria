"""Process-scoped SQLAlchemy engines and async session factories."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from oria.config.models import ResolvedRuntimeConfig
from oria.storage.urls import sqlalchemy_postgres_url


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


_TENANT_INFO_KEY = "oria_tenant_id"
_TENANT_GUC = "oria.tenant_id"


def set_tenant_context(connection: Connection, tenant_id: str) -> None:
    """Establish the trusted RLS tenant identity for the current transaction.

    Only the service/repository layer may call this, with a tenant ID taken
    from the authenticated Context. SQL bind parameters never establish the
    identity. The transaction-local setting (`SET LOCAL`) ends with the
    transaction; one transaction cannot switch tenant.
    """

    if connection.dialect.name != "postgresql":
        return
    if not tenant_id:
        raise ValueError("tenant identity must be a non-empty string")
    current = connection.info.get(_TENANT_INFO_KEY)
    if current is not None:
        if current != tenant_id:
            raise InvalidRequestError("one transaction cannot switch tenant context")
        return
    connection.exec_driver_sql("SELECT set_config(%s, %s, true)", (_TENANT_GUC, tenant_id))
    connection.info[_TENANT_INFO_KEY] = tenant_id


async def set_session_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Set the trusted RLS tenant identity on the session's transaction."""

    connection = await session.connection()
    await connection.run_sync(set_tenant_context, tenant_id)


def _clear_tenant_context(connection: Connection) -> None:
    connection.info.pop(_TENANT_INFO_KEY, None)


def _clear_pooled_tenant_context(dbapi_connection: Any, connection_record: Any) -> None:
    del dbapi_connection
    connection_record.info.pop(_TENANT_INFO_KEY, None)


def _create_postgres_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, pool_pre_ping=True)
    event.listen(engine.sync_engine, "begin", _clear_tenant_context)
    event.listen(engine.sync_engine, "commit", _clear_tenant_context)
    event.listen(engine.sync_engine, "rollback", _clear_tenant_context)
    event.listen(engine.sync_engine.pool, "checkin", _clear_pooled_tenant_context)
    return engine


class DatabaseResources:
    """The only process resource used to create local SQL repositories."""

    __slots__ = (
        "_business_engine",
        "_platform_engine",
        "business_backend",
        "business_sessions",
        "platform_backend",
        "platform_sessions",
    )

    def __init__(self, config: ResolvedRuntimeConfig) -> None:
        paths = config.data_paths
        self.platform_backend = config.storage.platform_db
        self.business_backend = config.storage.biz_db
        if config.storage.platform_db == "postgres":
            if config.storage.platform_url is None:
                raise ValueError("platform PostgreSQL URL is unavailable")
            self._platform_engine = _create_postgres_engine(
                sqlalchemy_postgres_url(config.storage.platform_url)
            )
        else:
            self._platform_engine = _create_sqlite_engine(paths.platform_db)
        if config.storage.biz_db == "postgres":
            if config.storage.business_url is None:
                raise ValueError("business PostgreSQL URL is unavailable")
            self._business_engine = _create_postgres_engine(
                sqlalchemy_postgres_url(config.storage.business_url)
            )
        else:
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
