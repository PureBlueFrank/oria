"""Security coverage for immutable launch approval bindings and tenant isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.launch import context, launch_harness, principal

from oria.core.types import Principal
from oria.domain.ledger import LaunchPlan

pytestmark = pytest.mark.security

HASH_Z = f"sha256:{'f' * 64}"


def _mutate_plan(plan: LaunchPlan, field: str, value: str) -> LaunchPlan:
    values = plan.model_dump()
    values[field] = value
    values["plan_hash"] = LaunchPlan.compute_plan_hash(
        campaign_draft_id=str(values["campaign_draft_id"]),
        campaign_draft_hash=str(values["campaign_draft_hash"]),
        rule_snapshot_id=str(values["rule_snapshot_id"]),
        rule_snapshot_hash=str(values["rule_snapshot_hash"]),
        coupon_batch_draft_id=str(values["coupon_batch_draft_id"]),
        coupon_batch_draft_hash=str(values["coupon_batch_draft_hash"]),
        merchant_scope_hash=str(values["merchant_scope_hash"]),
        material_version=str(values["material_version"]),
        child_steps=plan.child_steps,
        compensation_policy_version=str(values["compensation_policy_version"]),
    )
    return LaunchPlan.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("rule_snapshot_hash", HASH_Z, id="rule-snapshot"),
        pytest.param("campaign_draft_hash", HASH_Z, id="campaign-draft"),
        pytest.param("coupon_batch_draft_hash", HASH_Z, id="coupon-tier"),
        pytest.param("merchant_scope_hash", HASH_Z, id="merchant-scope"),
        pytest.param("material_version", "material-v2", id="material"),
        pytest.param(
            "compensation_policy_version",
            "compensation-v2",
            id="compensation-policy",
        ),
    ],
)
@pytest.mark.asyncio
async def test_rejects_tampered_launch_plan_binding(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    async with launch_harness(tmp_path) as harness:
        request = harness.request.model_copy(
            update={"plan": _mutate_plan(harness.request.plan, field, value)}
        )

        with pytest.raises(PermissionError):
            await harness.service.execute_launch(
                request,
                harness.admin_ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.platform_sessions() as session:
            status = await session.scalar(
                text("SELECT status FROM approvals WHERE approval_id = 'approval-launch-1'")
            )

    assert status == "invalidated"
    assert harness.coupon_adapter.materialize_calls == []
    assert harness.recruitment_adapter.publish_calls == []


@pytest.mark.asyncio
async def test_rejects_tampered_launch_child_arguments_and_invalidates_approval(
    tmp_path: Path,
) -> None:
    async with launch_harness(tmp_path) as harness:
        changed_args = harness.request.materialize_args.model_copy(
            update={"coupon_spec_hash": HASH_Z}
        )
        request = harness.request.model_copy(update={"materialize_args": changed_args})

        with pytest.raises(PermissionError):
            await harness.service.execute_launch(
                request,
                harness.admin_ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.platform_sessions() as session:
            status = await session.scalar(
                text("SELECT status FROM approvals WHERE approval_id = 'approval-launch-1'")
            )

    assert status == "invalidated"
    assert harness.coupon_adapter.materialize_calls == []


@pytest.mark.asyncio
async def test_rejects_changed_approval_subject_and_invalidates_binding(tmp_path: Path) -> None:
    async with launch_harness(tmp_path) as harness:
        with pytest.raises(PermissionError, match="requester"):
            await harness.service.execute_launch(
                harness.request,
                harness.other_admin_ctx,  # type: ignore[arg-type]
            )
        async with harness.databases.platform_sessions() as session:
            status = await session.scalar(
                text("SELECT status FROM approvals WHERE approval_id = 'approval-launch-1'")
            )

    assert status == "invalidated"
    assert harness.coupon_adapter.materialize_calls == []


@pytest.mark.asyncio
async def test_unapproved_launch_cannot_materialize_or_publish(tmp_path: Path) -> None:
    async with launch_harness(tmp_path, approve=False) as harness:
        with pytest.raises(PermissionError, match="not approved"):
            await harness.service.execute_launch(
                harness.request,
                harness.admin_ctx,  # type: ignore[arg-type]
            )
        saga = await harness.launch_repository.get_saga(
            harness.draft.campaign.campaign_id,
            harness.admin_ctx,  # type: ignore[arg-type]
        )

    assert saga is None
    assert harness.coupon_adapter.materialize_calls == []
    assert harness.recruitment_adapter.publish_calls == []


@pytest.mark.asyncio
async def test_launch_requester_cannot_self_approve(tmp_path: Path) -> None:
    async with launch_harness(tmp_path, approve=False) as harness:
        with pytest.raises(PermissionError, match="cannot decide"):
            await harness.service.decide_launch_approval(
                approval_id=harness.binding.approval.approval_id,
                decision="approve",
                reason=None,
                ctx=harness.admin_ctx,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_cross_tenant_launch_resume_is_denied_without_side_effects(tmp_path: Path) -> None:
    async with launch_harness(tmp_path) as harness:
        other_actor = principal(
            "tenant-b-admin",
            "campaign_admin",
            tenant_id="tenant-b",
        )
        other_ctx = context(other_actor, harness.policy)
        other_ctx.executor = Principal(
            subject_id="tenant-b-worker",
            tenant_id="tenant-b",
            kind="service",
            roles=("runtime",),
            authn_method="trusted-test-profile",
        )

        with pytest.raises(PermissionError):
            await harness.service.execute_launch(
                harness.request,
                other_ctx,  # type: ignore[arg-type]
            )

    assert harness.coupon_adapter.materialize_calls == []
    assert harness.recruitment_adapter.publish_calls == []
