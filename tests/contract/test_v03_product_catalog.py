"""V0.3-T05 product query snapshot/cursor and rule-binding contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.enrollment import TENANT, enrollment_harness, product

from oria.tools.models import QueryEligibleProductsParams

pytestmark = pytest.mark.contract


def _request(*, cursor: str | None = None, limit: int = 1) -> QueryEligibleProductsParams:
    return QueryEligibleProductsParams(
        campaign_id="campaign-1",
        rule_snapshot_id="rs_123456789012345678901234",
        product_circle_policy_ref="synthetic-product-circle-policy",
        product_circle_policy_version="1.0.0",
        cursor=cursor,
        limit=limit,
    )


@pytest.mark.asyncio
async def test_product_pages_replay_one_catalog_snapshot_after_current_snapshot_changes(
    tmp_path: Path,
) -> None:
    old_products = (product("product-1"), product("product-2"), product("product-3"))
    async with enrollment_harness(tmp_path, products=old_products) as harness:
        first = await harness.query.query(_request(), harness.ctx)  # type: ignore[arg-type]
        assert first.next_cursor is not None
        harness.catalog.install_snapshot(
            "catalog-snapshot-v2",
            {TENANT: (product("replacement-product"),)},
        )
        second = await harness.query.query(
            _request(cursor=first.next_cursor),
            harness.ctx,  # type: ignore[arg-type]
        )
        retried = await harness.query.query(
            _request(cursor=first.next_cursor),
            harness.ctx,  # type: ignore[arg-type]
        )
        assert second.next_cursor is not None
        third = await harness.query.query(
            _request(cursor=second.next_cursor),
            harness.ctx,  # type: ignore[arg-type]
        )

    assert first.catalog_snapshot_id == second.catalog_snapshot_id == third.catalog_snapshot_id
    assert first.catalog_snapshot_id == "catalog-snapshot-v1"
    assert second == retried
    assert tuple(page.products[0].product_ref for page in (first, second, third)) == (
        "product-1",
        "product-2",
        "product-3",
    )


def test_product_query_forbids_caller_controlled_merchant_candidates() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        QueryEligibleProductsParams.model_validate(
            _request().model_dump() | {"merchant_ids": ["demo-m004"]}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"rule_snapshot_id": "rs_aaaaaaaaaaaaaaaaaaaaaaaa"},
        {"product_circle_policy_ref": "caller-policy"},
        {"product_circle_policy_version": "caller-version"},
    ],
)
async def test_product_query_rejects_every_rule_snapshot_or_policy_binding_mismatch(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        with pytest.raises((LookupError, PermissionError), match=r"snapshot|policy"):
            await harness.query.query(
                _request().model_copy(update=updates),
                harness.ctx,  # type: ignore[arg-type]
            )
