"""Idempotent V0.1 local data initialization."""

from __future__ import annotations

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import ValueModel
from oria.migrations.runner import MigrationError, MigrationResult, upgrade_databases
from oria.resources.loader import (
    PackageAssetError,
    load_demo_data,
    verify_package_assets,
)
from oria.storage.database import DatabaseResources
from oria.storage.repositories import MerchantRepositoryError, SQLiteMerchantRepository


class DataInitializationError(RuntimeError):
    """Safe initialization error for the CLI boundary."""


class DataInitializationResult(ValueModel):
    initialized: bool = True
    dataset_version: str
    platform_revision: str
    business_revision: str
    merchants_inserted: int
    saver_setup: bool


async def initialize_data(config: ResolvedRuntimeConfig) -> DataInitializationResult:
    """Verify assets, upgrade both DBs, seed merchants, and call official saver setup."""
    try:
        verify_package_assets()
        bundle = load_demo_data()
        revisions: MigrationResult = upgrade_databases(config)
        async with DatabaseResources(config) as databases:
            repository = SQLiteMerchantRepository(databases.business_sessions)
            inserted = await repository.seed(bundle.merchants)
        async with AsyncSqliteSaver.from_conn_string(str(config.data_paths.platform_db)) as saver:
            await saver.setup()
    except (
        PackageAssetError,
        MigrationError,
        MerchantRepositoryError,
        aiosqlite.Error,
        OSError,
    ) as exc:
        raise DataInitializationError("local data initialization failed closed") from exc
    return DataInitializationResult(
        dataset_version=bundle.manifest.version,
        platform_revision=revisions.platform_revision,
        business_revision=revisions.business_revision,
        merchants_inserted=inserted,
        saver_setup=True,
    )
