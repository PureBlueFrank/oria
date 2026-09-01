"""Product catalog Adapter contract and deterministic in-memory implementation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from pydantic import Field

from oria.core.types import ValueModel
from oria.domain.product_eligibility import ProductSnapshot


class ProductCatalogPolicyBinding(ValueModel):
    policy_ref: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)


class ProductCatalogPage(ValueModel):
    catalog_snapshot_id: str = Field(min_length=1)
    products: tuple[ProductSnapshot, ...]
    next_cursor: str | None = None


class ProductCatalogAdapter(Protocol):
    async def list_products(
        self,
        *,
        tenant_id: str,
        merchant_ids: tuple[str, ...],
        policy: ProductCatalogPolicyBinding,
        catalog_snapshot_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> ProductCatalogPage: ...


class InMemoryProductCatalogAdapter:
    """Synthetic catalog retaining old snapshots so cursors replay stable pages."""

    def __init__(
        self,
        products_by_tenant: Mapping[str, tuple[ProductSnapshot, ...]],
        *,
        catalog_snapshot_id: str = "catalog-snapshot-v1",
        cursor_secret: str = "oria-synthetic-product-cursor-v1",
    ) -> None:
        if not catalog_snapshot_id or not cursor_secret:
            raise ValueError("catalog snapshot identity and cursor secret are required")
        self._current_snapshot_id = catalog_snapshot_id
        self._snapshots: dict[str, dict[str, tuple[ProductSnapshot, ...]]] = {
            catalog_snapshot_id: {
                tenant_id: self._sorted(products)
                for tenant_id, products in products_by_tenant.items()
            }
        }
        self._secret = cursor_secret.encode("utf-8")

    def install_snapshot(
        self,
        catalog_snapshot_id: str,
        products_by_tenant: Mapping[str, tuple[ProductSnapshot, ...]],
    ) -> None:
        """Switch new reads while retaining old snapshots for existing cursors."""
        if not catalog_snapshot_id or catalog_snapshot_id in self._snapshots:
            raise ValueError("catalog snapshot identity must be new and non-empty")
        self._snapshots[catalog_snapshot_id] = {
            tenant_id: self._sorted(products) for tenant_id, products in products_by_tenant.items()
        }
        self._current_snapshot_id = catalog_snapshot_id

    async def list_products(
        self,
        *,
        tenant_id: str,
        merchant_ids: tuple[str, ...],
        policy: ProductCatalogPolicyBinding,
        catalog_snapshot_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> ProductCatalogPage:
        if not tenant_id:
            raise PermissionError("trusted tenant context is required")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_merchants = tuple(sorted(set(merchant_ids)))
        if not normalized_merchants:
            raise ValueError("at least one merchant is required")
        query_hash = self._query_hash(normalized_merchants, policy)
        if cursor is None:
            snapshot_id = catalog_snapshot_id or self._current_snapshot_id
            offset = 0
        else:
            snapshot_id, offset, observed_hash = self._decode_cursor(cursor)
            if observed_hash != query_hash:
                raise ValueError("product cursor does not match this query")
            if catalog_snapshot_id is not None and snapshot_id != catalog_snapshot_id:
                raise ValueError("product cursor does not match the requested catalog snapshot")
        try:
            tenant_products = self._snapshots[snapshot_id].get(tenant_id, ())
        except KeyError as exc:
            raise ValueError("product cursor references an unavailable catalog snapshot") from exc
        allowed_merchants = frozenset(normalized_merchants)
        products = tuple(
            product for product in tenant_products if product.merchant_id in allowed_merchants
        )
        page = products[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            self._encode_cursor(snapshot_id, next_offset, query_hash)
            if next_offset < len(products)
            else None
        )
        return ProductCatalogPage(
            catalog_snapshot_id=snapshot_id,
            products=page,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _sorted(products: tuple[ProductSnapshot, ...]) -> tuple[ProductSnapshot, ...]:
        keys = [(item.merchant_id, item.product_ref, item.product_version) for item in products]
        if len(keys) != len(set(keys)):
            raise ValueError("synthetic product snapshot contains duplicate business keys")
        return tuple(
            sorted(
                products,
                key=lambda item: (item.merchant_id, item.product_ref, item.product_version),
            )
        )

    @staticmethod
    def _query_hash(
        merchant_ids: tuple[str, ...],
        policy: ProductCatalogPolicyBinding,
    ) -> str:
        payload = json.dumps(
            {
                "merchant_ids": merchant_ids,
                "policy_ref": policy.policy_ref,
                "policy_version": policy.policy_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _encode_cursor(self, snapshot_id: str, offset: int, query_hash: str) -> str:
        payload = json.dumps(
            {"offset": offset, "query_hash": query_hash, "snapshot_id": snapshot_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hashlib.sha256(self._secret + payload).hexdigest()
        return f"pc1.{encoded}.{signature}"

    def _decode_cursor(self, cursor: str) -> tuple[str, int, str]:
        try:
            prefix, encoded, signature = cursor.split(".", 2)
            if prefix != "pc1":
                raise ValueError
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if hashlib.sha256(self._secret + payload).hexdigest() != signature:
                raise ValueError
            decoded = json.loads(payload)
            snapshot_id = str(decoded["snapshot_id"])
            offset = int(decoded["offset"])
            query_hash = str(decoded["query_hash"])
            if offset < 0 or not snapshot_id or len(query_hash) != 64:
                raise ValueError
            return snapshot_id, offset, query_hash
        except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("product cursor is invalid") from exc
