"""V0.3-T05 tenant and caller-controlled product-policy boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from tests.support.enrollment import EXECUTOR, NOW, auto_command, enrollment_harness, product

from oria.core.integration_events import IntegrationInboxRecord, parse_integration_event
from oria.core.types import Principal
from oria.domain.enrollment import (
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    MerchantEnrollmentCommand,
    UpsertEnrollmentItemsArgs,
)
from oria.permission.local import LocalPolicyEngine
from oria.tools.enrollment import UpsertEnrollmentItemsTool
from oria.tools.models import QueryEligibleProductsParams

pytestmark = pytest.mark.security


def query_params(**updates: object) -> QueryEligibleProductsParams:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
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
        items=(
            EnrollmentItemInput(
                merchant_id="demo-m001",
                product_ref="product-1",
                product_version="v1",
            ),
        ),
    )


def test_upsert_tool_contract_forbids_caller_controlled_source_and_idempotency_key() -> None:
    payload = upsert_args().model_dump(mode="json")
    for field, value in (("source", "merchant"), ("idempotency_key", "caller-key")):
        with pytest.raises(ValidationError, match="Extra inputs"):
            UpsertEnrollmentItemsArgs.model_validate(payload | {field: value})


@pytest.mark.asyncio
async def test_integration_adapter_cannot_forge_auto_circle_source(tmp_path: Path) -> None:
    adapter = Principal(
        subject_id="merchant-adapter",
        tenant_id="local-community",
        kind="service",
        roles=("integration_adapter",),
        authn_method="trusted-test-profile",
    )
    async with enrollment_harness(tmp_path, actor=adapter) as harness:
        with pytest.raises(PermissionError, match="not authorized"):
            await harness.enrollments.upsert_auto(
                auto_command(upsert_args().items, circle_run_id="forged-auto"),
                harness.ctx,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_campaign_admin_cannot_forge_merchant_source_through_tool(tmp_path: Path) -> None:
    async with enrollment_harness(tmp_path) as harness:
        command = auto_command(upsert_args().items, circle_run_id="admin-circle")
        tool = UpsertEnrollmentItemsTool(harness.enrollments, command.binding)
        with pytest.raises(ValidationError, match="Extra inputs"):
            await tool.run(
                upsert_args().model_dump(mode="json") | {"source": "merchant"},
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_items"))

    assert count == 0


@pytest.mark.asyncio
async def test_merchant_command_rejects_unpersisted_payload_hash_binding(tmp_path: Path) -> None:
    event = parse_integration_event(
        {
            "schema_version": 1,
            "event_type": "merchant.enrollment_upserted",
            "tenant_id": "local-community",
            "adapter_id": "merchant-adapter",
            "source_event_id": "forged-merchant-event",
            "signature_subject": "adapter-principal",
            "version": 1,
            "payload": {
                "campaign_id": "campaign-1",
                "enrollment_id": "caller-enrollment",
                "merchant_id": "demo-m001",
                "product_ref": "product-1",
                "product_version": "v1",
            },
        }
    )
    record = IntegrationInboxRecord(
        tenant_id="local-community",
        adapter_id="merchant-adapter",
        source_event_id="forged-merchant-event",
        event_type="merchant.enrollment_upserted",
        resource_version=1,
        signature_subject="adapter-principal",
        redacted_payload={},
        payload_hash="sha256:" + "f" * 64,
        processing_status="matched",
        wait_id="trusted-wait",
        received_at=NOW,
        processed_at=NOW,
    )
    async with enrollment_harness(tmp_path) as harness:
        with pytest.raises(PermissionError, match="source binding is not trusted"):
            await harness.enrollments.upsert_merchant(
                MerchantEnrollmentCommand(event=event, inbox_record=record),  # type: ignore[arg-type]
                harness.ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.business_sessions() as session:
            count = await session.scalar(text("SELECT COUNT(*) FROM enrollment_items"))

    assert count == 0


def test_query_contract_forbids_caller_supplied_price_category_keyword_or_status_filters() -> None:
    base = query_params().model_dump()
    for field, value in (
        ("product_price_max", "999999"),
        ("product_categories", ["caller-category"]),
        ("keywords", ["caller-keyword"]),
        ("available", True),
        ("merchant_ids", ["demo-m004"]),
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            QueryEligibleProductsParams.model_validate(base | {field: value})


@pytest.mark.asyncio
async def test_product_query_uses_server_computed_merchant_eligibility_and_whitelist_projection(
    tmp_path: Path,
) -> None:
    products = (
        product(),
        product("forged-product", merchant_id="demo-m004"),
    )
    async with enrollment_harness(tmp_path, products=products) as harness:
        result = await harness.query.query(
            query_params(),
            harness.ctx,  # type: ignore[arg-type]
        )

    assert tuple(item.merchant_id for item in result.products) == ("demo-m001",)
    visible_fields = set(result.products[0].model_dump())
    assert visible_fields == {
        "candidate_ref",
        "merchant_id",
        "product_ref",
        "product_version",
        "category",
        "normalized_price",
        "currency",
        "normalized_title",
        "keyword_labels",
    }
    assert "eligibility_facts" not in result.model_dump_json()
    assert "source_ref" not in result.model_dump_json()


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
            await harness.enrollments.upsert_auto(
                auto_command(upsert_args().items, circle_run_id="auto-1"),
                harness.ctx,  # type: ignore[arg-type]
            )

    assert queried.products == ()
    assert queried.exclusion_reason_counts == {"product_unavailable": 1}


@pytest.mark.asyncio
async def test_cross_tenant_campaign_product_enrollment_and_coupon_resources_are_unavailable(
    tmp_path: Path,
) -> None:
    async with enrollment_harness(tmp_path, confirmation_steps=()) as harness:
        local = await harness.enrollments.upsert_auto(
            auto_command(upsert_args().items, circle_run_id="auto-1"),
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
            await harness.enrollments.upsert_auto(
                auto_command(upsert_args().items, circle_run_id="auto-other"),
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
