"""V0.3-T05 enrollment upsert and coupon-link idempotency contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import enrollment_harness

from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    UpsertEnrollmentItemsArgs,
)
from oria.tools.enrollment import LinkCouponBatchTool, UpsertEnrollmentItemsTool
from oria.tools.product_catalog import QueryEligibleProductsTool

pytestmark = pytest.mark.contract


def _upsert(source: str, idempotency_key: str) -> UpsertEnrollmentItemsArgs:
    return UpsertEnrollmentItemsArgs(
        campaign_id="campaign-1",
        source=source,  # type: ignore[arg-type]
        items=(
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        ),
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_upsert_replays_same_execution_and_merges_two_sources_into_one_item(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        auto = await harness.enrollments.upsert_items(
            _upsert("auto", "auto-event-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        repeated = await harness.enrollments.upsert_items(
            _upsert("auto", "auto-event-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        merchant = await harness.enrollments.upsert_items(
            _upsert("merchant", "merchant-event-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM enrollment_items), "
                        "(SELECT COUNT(*) FROM confirmation_tasks), "
                        "(SELECT COUNT(*) FROM product_snapshots), "
                        "(SELECT COUNT(*) FROM tool_executions WHERE "
                        "tool_name = 'upsert_enrollment_items')"
                    )
                )
            ).one()

    assert auto.execution_id == repeated.execution_id
    assert auto.enrollment_items[0].sources == frozenset({"auto"})
    assert merchant.enrollment_items[0].sources == frozenset({"auto", "merchant"})
    assert counts == (1, 3, 1, 2)


@pytest.mark.asyncio
async def test_coupon_link_replays_one_unique_active_link(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        upserted = await harness.enrollments.upsert_items(
            _upsert("auto", "auto-event-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = upserted.enrollment_items[0].enrollment_item_id
        request = LinkCouponBatchArgs(
            enrollment_item_ids=(item_id,),
            coupon_batch_id="coupon-1",
            tier_mapping={item_id: "base"},
            idempotency_key="link-event-1",
        )

        first = await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]
        repeated = await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_coupon_links"))

    assert first == repeated
    assert first.links[0].status == "active"
    assert count == 1


@pytest.mark.asyncio
async def test_t05_tool_contracts_return_schema_valid_results_without_fixed_hitl(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        query_tool = QueryEligibleProductsTool(harness.query)
        query_result = await query_tool.run(
            {
                "campaign_id": "campaign-1",
                "merchant_ids": ["demo-m001"],
                "rule_snapshot_id": harness.snapshot.snapshot_id,
                "product_circle_policy_ref": "synthetic-product-circle-policy",
                "product_circle_policy_version": "1.0.0",
                "limit": 100,
            },
            harness.ctx,  # type: ignore[arg-type]
        )
        upsert_tool = UpsertEnrollmentItemsTool(harness.enrollments)
        upsert_result = await upsert_tool.run(
            _upsert("auto", "auto-tool-event").model_dump(mode="json"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = str(upsert_result.data["enrollment_items"][0]["enrollment_item_id"])  # type: ignore[index]
        link_tool = LinkCouponBatchTool(harness.links)
        link_result = await link_tool.run(
            {
                "enrollment_item_ids": [item_id],
                "coupon_batch_id": "coupon-1",
                "tier_mapping": {item_id: "base"},
                "idempotency_key": "link-tool-event",
            },
            harness.ctx,  # type: ignore[arg-type]
        )

    assert query_result.ok and len(query_result.data["products"]) == 2  # type: ignore[arg-type]
    assert upsert_result.ok and upsert_tool.policy.approval_mode == "none"
    assert link_result.ok and link_tool.policy.approval_mode == "none"


@pytest.mark.asyncio
async def test_coupon_link_rejects_tier_not_present_in_frozen_benefit_policy(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(
        tmp_path, confirmation_steps=(), benefit_tiers=("base",)
    ) as harness:
        upserted = await harness.enrollments.upsert_items(
            _upsert("auto", "auto-base-only"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = upserted.enrollment_items[0].enrollment_item_id

        with pytest.raises(ValueError, match="frozen rule"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(item_id,),
                    coupon_batch_id="coupon-1",
                    tier_mapping={item_id: "boosted"},
                    idempotency_key="invalid-tier",
                ),
                harness.ctx,  # type: ignore[arg-type]
            )
