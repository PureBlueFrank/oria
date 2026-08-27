"""SQLite implementations of domain Repository Protocols."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.domain.models import MerchantRecord, MerchantSeedSet


class MerchantRepositoryError(RuntimeError):
    """Safe repository failure that does not expose SQL or restricted values."""


class SQLiteMerchantRepository:
    """Read and seed tenant-qualified synthetic merchant facts."""

    __slots__ = ("__sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.__sessions = sessions

    async def list_for_eligibility(self, ctx: object) -> tuple[MerchantRecord, ...]:
        tenant_id = getattr(ctx, "tenant_id", None)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise MerchantRepositoryError("trusted tenant context is required")
        try:
            async with self.__sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT tenant_id, merchant_id, version, display_name, categories_json, "
                        "cities_json, enrollment_systems_json, sales_org_code, active "
                        "FROM merchants WHERE tenant_id = :tenant_id ORDER BY merchant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                rows = result.mappings().all()
            return tuple(
                MerchantRecord(
                    tenant_id=str(row["tenant_id"]),
                    merchant_id=str(row["merchant_id"]),
                    version=int(row["version"]),
                    display_name=str(row["display_name"]),
                    categories=tuple(json.loads(str(row["categories_json"]))),
                    cities=tuple(json.loads(str(row["cities_json"]))),
                    enrollment_systems=tuple(json.loads(str(row["enrollment_systems_json"]))),
                    sales_org_code=str(row["sales_org_code"]),
                    active=bool(row["active"]),
                )
                for row in rows
            )
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MerchantRepositoryError("merchant repository read failed") from exc

    async def seed(self, seed_set: MerchantSeedSet) -> int:
        now = datetime.now(UTC)
        inserted = 0
        try:
            async with self.__sessions.begin() as session:
                existing_result = await session.execute(
                    text(
                        "SELECT merchant_id, version, display_name, categories_json, cities_json, "
                        "enrollment_systems_json, sales_org_code, active FROM merchants "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": seed_set.tenant_id},
                )
                existing = {str(row["merchant_id"]): row for row in existing_result.mappings()}
                for merchant in seed_set.merchants:
                    canonical = {
                        "version": merchant.version,
                        "display_name": merchant.display_name,
                        "categories_json": _canonical_json(merchant.categories),
                        "cities_json": _canonical_json(merchant.cities),
                        "enrollment_systems_json": _canonical_json(merchant.enrollment_systems),
                        "sales_org_code": merchant.internal_sales_org_code(),
                        "active": merchant.active,
                    }
                    row = existing.get(merchant.merchant_id)
                    if row is not None:
                        observed = {name: row[name] for name in canonical}
                        observed["active"] = bool(observed["active"])
                        if observed != canonical:
                            raise MerchantRepositoryError(
                                "existing demo merchant data conflicts with the installed version"
                            )
                        continue
                    await session.execute(
                        text(
                            "INSERT INTO merchants "
                            "(tenant_id, merchant_id, version, display_name, categories_json, "
                            "cities_json, enrollment_systems_json, sales_org_code, active, "
                            "created_at, updated_at) VALUES "
                            "(:tenant_id, :merchant_id, :version, :display_name, :categories_json, "
                            ":cities_json, :enrollment_systems_json, :sales_org_code, :active, "
                            ":created_at, :updated_at)"
                        ),
                        {
                            "tenant_id": seed_set.tenant_id,
                            "merchant_id": merchant.merchant_id,
                            **canonical,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    inserted += 1
        except MerchantRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise MerchantRepositoryError("merchant repository seed failed") from exc
        return inserted


def _canonical_json(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
