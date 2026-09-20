"""Idempotent V0.1 local data initialization."""

from __future__ import annotations

import aiosqlite
from psycopg import Error as PsycopgError

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import ValueModel
from oria.migrations.runner import MigrationError, MigrationResult, upgrade_databases
from oria.orchestrator.checkpoint import open_tenant_saver
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
        if revisions.platform_revision is None or revisions.business_revision is None:
            raise MigrationError("data initialization requires both migration chains")
        async with DatabaseResources(config) as databases:
            repository = SQLiteMerchantRepository(databases.business_sessions)
            inserted = await repository.seed(bundle.merchants)
        async with open_tenant_saver(config):
            pass
    except (
        PackageAssetError,
        MigrationError,
        MerchantRepositoryError,
        PsycopgError,
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
