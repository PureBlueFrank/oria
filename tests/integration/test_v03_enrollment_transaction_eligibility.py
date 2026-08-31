"""Final-transaction merchant and product hard-eligibility replay tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.enrollment import auto_command, enrollment_harness, product

from oria.domain.enrollment import EnrollmentItemInput, LinkCouponBatchArgs
from oria.storage.repositories import BusinessRepositoryError

pytestmark = pytest.mark.integration


def _items() -> tuple[EnrollmentItemInput, ...]:
    return (
        EnrollmentItemInput(
            merchant_id="demo-m001",
            product_ref="product-1",
            product_version="v1",
        ),
    )


@pytest.mark.asyncio
async def test_committed_merchant_deactivation_between_precheck_and_final_transaction_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        ledger = harness.enrollments._ledger
        original = ledger.record_local_success

        async def deactivate_then_commit(*args: object, **kwargs: object):
            async with harness.databases.business_sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE merchants SET active = 0, version = version + 1 WHERE tenant_id "
                        "= 'local-community' AND merchant_id = 'demo-m001'"
                    )
                )
            return await original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ledger, "record_local_success", deactivate_then_commit)
        with pytest.raises(BusinessRepositoryError, match="no longer satisfies"):
            await harness.enrollments.upsert_auto(
                auto_command(_items(), circle_run_id="merchant-deactivated"),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            item_count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_items"))
            merchant_active = await session.scalar(
                text(
                    "SELECT active FROM merchants WHERE tenant_id = 'local-community' AND "
                    "merchant_id = 'demo-m001'"
                )
            )

    assert item_count == 0
    assert merchant_active == 0


@pytest.mark.asyncio
async def test_final_transaction_rejects_product_snapshot_item_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with enrollment_harness(tmp_path) as harness:
        repository = harness.workflow_repository
        original = repository.upsert_enrollment_items

        async def corrupt_bundle(session: object, **kwargs: object) -> None:
            bundles = kwargs["bundles"]
            product, enrollment, item, tasks = bundles[0]  # type: ignore[index]
            kwargs["bundles"] = (
                (
                    product.model_copy(update={"merchant_id": "demo-m002"}),
                    enrollment,
                    item,
                    tasks,
                ),
            )
            await original(session, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(repository, "upsert_enrollment_items", corrupt_bundle)
        with pytest.raises(BusinessRepositoryError, match="does not match enrollment item"):
            await harness.enrollments.upsert_auto(
                auto_command(_items(), circle_run_id="wrong-product-bundle"),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM product_snapshots), "
                        "(SELECT COUNT(*) FROM enrollment_items)"
                    )
                )
            ).one()

    assert counts == (0, 0)


@pytest.mark.asyncio
async def test_coupon_link_replays_persisted_hard_policy_for_existing_confirmed_item(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        enrolled = await harness.enrollments.upsert_auto(
            auto_command(_items(), circle_run_id="existing-confirmed-item"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = enrolled.enrollment_items[0].enrollment_item_id
        async with harness.databases.business_sessions.begin() as session:
            attributes_json = await session.scalar(
                text("SELECT attributes_json FROM product_snapshots LIMIT 1")
            )
            attributes = json.loads(str(attributes_json))
            attributes["eligibility_facts"] = {"available": False, "status": "off"}
            attributes["sellability_snapshot"]["available"] = False
            attributes["sellability_snapshot"]["status"] = "off"
            await session.execute(
                text("UPDATE product_snapshots SET attributes_json = :attributes_json"),
                {
                    "attributes_json": json.dumps(
                        attributes,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                },
            )

        with pytest.raises(BusinessRepositoryError, match="does not satisfy hard policy"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(item_id,),
                    coupon_batch_id="coupon-1",
                    tier_mapping={item_id: "base"},
                    idempotency_key="ineligible-existing-confirmed",
                ),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            link_count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_coupon_links"))
            link_version = await session.scalar(
                text("SELECT link_version FROM campaign_approval_bindings")
            )

    assert link_count == 0
    assert link_version == 0


@pytest.mark.asyncio
async def test_coupon_link_separately_rejects_current_catalog_unsellability(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        enrolled = await harness.enrollments.upsert_auto(
            auto_command(_items(), circle_run_id="frozen-eligible-current-off"),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = enrolled.enrollment_items[0].enrollment_item_id
        harness.catalog.install_snapshot(
            "catalog-snapshot-v2",
            {"local-community": (product(available=False),)},
        )

        with pytest.raises(ValueError, match="not currently sellable"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(item_id,),
                    coupon_batch_id="coupon-1",
                    tier_mapping={item_id: "base"},
                    idempotency_key="current-catalog-off",
                ),
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            frozen_status = await session.scalar(
                text(
                    "SELECT json_extract(attributes_json, '$.eligibility_facts.status') "
                    "FROM product_snapshots"
                )
            )
            link_count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_coupon_links"))

    assert frozen_status == "available"
    assert link_count == 0
