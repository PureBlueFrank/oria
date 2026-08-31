"""V0.3-T05 tenant and caller-controlled product-policy boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from tests.support.enrollment import EXECUTOR, enrollment_harness, product

from oria.core.types import Principal
from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    UpsertEnrollmentItemsArgs,
)
from oria.permission.local import LocalPolicyEngine
from oria.tools.models import QueryEligibleProductsParams

pytestmark = pytest.mark.security


def query_params(**updates: object) -> QueryEligibleProductsParams:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
        "merchant_ids": ("demo-m001",),
        "rule_snapshot_id": "rs_123456789012345678901234",
        "product_circle_policy_ref": "synthetic-product-circle-policy",
        "product_circle_policy_version": "1.0.0",
        "limit": 100,
    }
    values.update(updates)
    return QueryEligibleProductsParams.model_validate(values)


def upsert_args() -> UpsertEnrollmentItemsArgs:
    return UpsertEnrollmentItemsArgs(
        campaign_id="campaign-1",
        source="auto",
        items=(
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        ),
        idempotency_key="auto-1",
    )


def test_query_contract_forbids_caller_supplied_price_category_keyword_or_status_filters() -> None:
    base = query_params().model_dump()
    for field, value in (
        ("product_price_max", "999999"),
        ("product_categories", ["caller-category"]),
        ("keywords", ["caller-keyword"]),
        ("available", True),
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            QueryEligibleProductsParams.model_validate(base | {field: value})


@pytest.mark.asyncio
async def test_query_and_upsert_enforce_the_same_frozen_unavailable_product_rule(
    tmp_path: Path,
) -> None:
    unavailable = product(available=False)
    async with enrollment_harness(tmp_path, products=(unavailable,)) as harness:
        queried = await harness.query.query(
            query_params(),
            harness.ctx,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="hard policy"):
            await harness.enrollments.upsert_items(
                upsert_args(),
                harness.ctx,  # type: ignore[arg-type]
            )

    assert queried.products == ()
    assert queried.exclusion_reason_counts == {"product_unavailable": 1}


@pytest.mark.asyncio
async def test_cross_tenant_campaign_product_enrollment_and_coupon_resources_are_unavailable(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        local = await harness.enrollments.upsert_items(
            upsert_args(),
            harness.ctx,  # type: ignore[arg-type]
        )
        item_id = local.enrollment_items[0].enrollment_item_id
        other_actor = Principal(
            subject_id="other-admin",
            tenant_id="other-tenant",
            kind="human",
            roles=("campaign_admin",),
            authn_method="trusted-test-profile",
        )
        other_executor = EXECUTOR.model_copy(update={"tenant_id": "other-tenant"})
        other_policy = LocalPolicyEngine(
            trusted_actors=(other_actor,), trusted_executors=(other_executor,)
        )
        other_ctx = SimpleNamespace(
            actor=other_actor,
            executor=other_executor,
            tenant_id="other-tenant",
            correlation_id="other-correlation",
            run_id="other-run",
            policy=other_policy,
        )

        with pytest.raises(LookupError, match="campaign"):
            await harness.query.query(query_params(), other_ctx)  # type: ignore[arg-type]
        with pytest.raises(LookupError, match="campaign"):
            await harness.enrollments.upsert_items(
                upsert_args(),
                other_ctx,  # type: ignore[arg-type]
            )
        with pytest.raises(LookupError, match="coupon batch"):
            await harness.links.link(
                LinkCouponBatchArgs(
                    enrollment_item_ids=(item_id,),
                    coupon_batch_id="coupon-1",
                    tier_mapping={item_id: "base"},
                    idempotency_key="cross-tenant-link",
                ),
                other_ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_coupon_links"))

    assert count == 0
