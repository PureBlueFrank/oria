"""V0.3-T05 enrollment upsert and coupon-link idempotency contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import auto_command, enrollment_harness

from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    UpsertEnrollmentItemsArgs,
)
from oria.tools.enrollment import LinkCouponBatchTool, UpsertEnrollmentItemsTool
from oria.tools.models import QueryEligibleProductsParams
from oria.tools.product_catalog import QueryEligibleProductsTool

pytestmark = pytest.mark.contract


def _upsert(product_ref: str = "product-1") -> UpsertEnrollmentItemsArgs:
    return UpsertEnrollmentItemsArgs(
        campaign_id="campaign-1",
        items=(
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref=product_ref,
                product_version="v1",
            ),
        ),
    )


def _auto(circle_run_id: str, product_ref: str = "product-1"):
    request = _upsert(product_ref)
    return auto_command(request.items, circle_run_id=circle_run_id)


@pytest.mark.asyncio
async def test_upsert_replays_same_execution_for_one_server_circle_run(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        auto = await harness.enrollments.upsert_auto(
            _auto("auto-event-1"),
            harness.ctx,  # type: ignore[arg-type]
        )
        repeated = await harness.enrollments.upsert_auto(
            _auto("auto-event-1"),
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
    assert counts == (1, 3, 1, 1)


@pytest.mark.asyncio
async def test_coupon_link_replays_one_unique_active_link(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        upserted = await harness.enrollments.upsert_auto(
            _auto("auto-event-1"),
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
async def test_upsert_history_precedes_changed_catalog_merchant_and_request_key_conflicts(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        request = _auto("request-key-1")
        first = await harness.enrollments.upsert_auto(
            request,
            harness.ctx,  # type: ignore[arg-type]
        )
        harness.catalog.install_snapshot("catalog-snapshot-v2", {"local-community": ()})
        async with harness.databases.business_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE merchants SET active = 0 WHERE tenant_id = 'local-community' "
                    "AND merchant_id = 'demo-m001'"
                )
            )

        replayed = await harness.enrollments.upsert_auto(
            request,
            harness.ctx,  # type: ignore[arg-type]
        )
        same_business_new_key = await harness.enrollments.upsert_auto(
            _auto("request-key-2"),
            harness.ctx,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="conflicts with canonical payload"):
            await harness.enrollments.upsert_auto(
                _auto("request-key-1", "product-2"),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM tool_executions WHERE "
                        "tool_name = 'upsert_enrollment_items'), "
                        "(SELECT COUNT(*) FROM tool_execution_requests WHERE "
                        "tool_name = 'upsert_enrollment_items'), "
                        "(SELECT COUNT(*) FROM domain_events WHERE "
                        "event_type = 'enrollment.items_upserted'), "
                        "(SELECT COUNT(*) FROM audit_events WHERE "
                        "action = 'upsert_enrollment_items'), "
                        "(SELECT COUNT(*) FROM outbox WHERE "
                        "topic = 'enrollment.items_upserted')"
                    )
                )
            ).one()

    assert replayed.execution_id == first.execution_id == same_business_new_key.execution_id
    assert counts == (1, 2, 1, 1, 1)


@pytest.mark.asyncio
async def test_auto_enrollment_reloads_server_issued_catalog_snapshot_after_catalog_advances(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        query = await harness.query.query(
            QueryEligibleProductsParams(
                campaign_id="campaign-1",
                rule_snapshot_id=harness.snapshot.snapshot_id,
                product_circle_policy_ref="synthetic-product-circle-policy",
                product_circle_policy_version="1.0.0",
                limit=100,
            ),
            harness.ctx,  # type: ignore[arg-type]
        )
        harness.catalog.install_snapshot("catalog-snapshot-v2", {"local-community": ()})
        request = _upsert()

        enrolled = await harness.enrollments.upsert_auto(
            auto_command(
                request.items,
                circle_run_id="server-issued-old-snapshot",
                catalog_snapshot_id=query.catalog_snapshot_id,
            ),
            harness.ctx,  # type: ignore[arg-type]
        )

    assert enrolled.enrollment_items[0].product_ref == "product-1"


@pytest.mark.asyncio
async def test_coupon_link_history_precedes_expiry_and_same_request_key_rejects_new_tier(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        upserted = await harness.enrollments.upsert_auto(
            _auto("auto-link-history"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = upserted.enrollment_items[0].enrollment_item_id
        request = LinkCouponBatchArgs(
            enrollment_item_ids=(item_id,),
            coupon_batch_id="coupon-1",
            tier_mapping={item_id: "base"},
            idempotency_key="link-history",
        )
        first = await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]
        async with harness.databases.business_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE coupon_batches SET status = 'expired' "
                    "WHERE coupon_batch_id = 'coupon-1'"
                )
            )
        replayed = await harness.links.link(request, harness.ctx)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="conflicts with canonical payload"):
            await harness.links.link(
                request.model_copy(update={"tier_mapping": {item_id: "boosted"}}),
                harness.ctx,  # type: ignore[arg-type]
            )

    assert replayed == first


@pytest.mark.asyncio
async def test_t05_tool_contracts_return_schema_valid_results_without_fixed_hitl(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        query_tool = QueryEligibleProductsTool(harness.query)
        query_result = await query_tool.run(
            {
                "campaign_id": "campaign-1",
                "rule_snapshot_id": harness.snapshot.snapshot_id,
                "product_circle_policy_ref": "synthetic-product-circle-policy",
                "product_circle_policy_version": "1.0.0",
                "limit": 100,
            },
            harness.ctx,  # type: ignore[arg-type]
        )
        command = _auto("auto-tool-event")
        upsert_tool = UpsertEnrollmentItemsTool(harness.enrollments, command.binding)
        upsert_result = await upsert_tool.run(
            _upsert().model_dump(mode="json"),
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
    serialized_products = str(query_result.data["products"])
    assert "eligibility_facts" not in serialized_products
    assert "source_ref" not in serialized_products
    assert upsert_result.ok and upsert_tool.policy.approval_mode == "none"
    assert link_result.ok and link_tool.policy.approval_mode == "none"
    assert upsert_result.idempotency_key == upsert_result.data["idempotency_key"]  # type: ignore[index]
    assert upsert_result.idempotency_key != upsert_result.data["request_idempotency_key"]  # type: ignore[index]
    assert link_result.idempotency_key == link_result.data["idempotency_key"]  # type: ignore[index]
    assert link_result.idempotency_key != link_result.data["request_idempotency_key"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_coupon_link_rejects_tier_not_present_in_frozen_benefit_policy(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(
        tmp_path, confirmation_steps=(), benefit_tiers=("base",)
    ) as harness:
        upserted = await harness.enrollments.upsert_auto(
            _auto("auto-base-only"),
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
