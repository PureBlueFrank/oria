"""Reusable local SQLite launch harness for T04 tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from oria.adapters.launch import InMemoryCouponBatchAdapter, InMemoryRecruitmentAdapter
from oria.config import resolve_runtime_config
from oria.core.approvals import ApprovalService
from oria.core.execution_ledger import ExecutionLedger
from oria.core.types import Principal
from oria.data import initialize_data
from oria.domain.launch import (
    CampaignDraft,
    CampaignDraftSpec,
    CompensationPolicyRegistry,
    DefaultCampaignLaunchService,
    LaunchApprovalBinding,
    LaunchExecutionRequest,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.permission.local import LocalPolicyEngine
from oria.rag.models import CampaignRuleSnapshot
from oria.resources.loader import load_demo_data
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import (
    SQLiteCampaignDraftRepository,
    SQLiteCampaignLaunchRepository,
)

NOW = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


class IdFactory:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}_{self.value}"


def principal(subject_id: str, *roles: str, tenant_id: str = "local-community") -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        kind="human",
        roles=roles,
        authn_method="trusted-test-profile",
    )


ADMIN = principal("campaign-admin", "campaign_admin")
OTHER_ADMIN = principal("other-campaign-admin", "campaign_admin")
APPROVER = principal("launch-approver", "launch_approver")
EXECUTOR = Principal(
    subject_id="launch-worker",
    tenant_id="local-community",
    kind="service",
    roles=("runtime",),
    authn_method="trusted-test-profile",
)


def context(actor: Principal, policy: LocalPolicyEngine) -> SimpleNamespace:
    return SimpleNamespace(
        actor=actor,
        executor=EXECUTOR,
        tenant_id=actor.tenant_id,
        correlation_id="launch-correlation",
        policy=policy,
    )


def snapshot() -> CampaignRuleSnapshot:
    rules = load_demo_data().rules
    placeholder = CampaignRuleSnapshot(
        snapshot_id="rs_123456789012345678901234",
        snapshot_hash="sha256:" + "0" * 64,
        tenant_id="local-community",
        effective_at=NOW,
        basic=rules.basic,
        recruitment_scope=rules.recruitment_scope,
        enrollment_policy=rules.enrollment_policy,
        benefit_policy=rules.benefit_policy,
        confirmation_policy=rules.confirmation_policy,
        merchant_material=rules.merchant_material,
        field_evidence={},
    )
    return placeholder.model_copy(update={"snapshot_hash": placeholder.recompute_hash()})


@dataclass(slots=True)
class LaunchHarness:
    databases: DatabaseResources
    service: DefaultCampaignLaunchService
    launch_repository: SQLiteCampaignLaunchRepository
    coupon_adapter: InMemoryCouponBatchAdapter
    recruitment_adapter: InMemoryRecruitmentAdapter
    policy: LocalPolicyEngine
    admin_ctx: SimpleNamespace
    approver_ctx: SimpleNamespace
    other_admin_ctx: SimpleNamespace
    draft: CampaignDraft
    binding: LaunchApprovalBinding
    request: LaunchExecutionRequest


@asynccontextmanager
async def launch_harness(
    tmp_path: Path,
    *,
    recruitment_status: Literal["accepted", "unknown", "rejected"] = "accepted",
    coupon_status: Literal["accepted", "unknown", "rejected"] = "accepted",
    compensation_contract_verified: bool = False,
    verified_compensation_policy: bool = False,
    approve: bool = True,
) -> AsyncIterator[LaunchHarness]:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    clock = Clock()
    ids = IdFactory()
    policy = LocalPolicyEngine(
        trusted_actors=(ADMIN, OTHER_ADMIN, APPROVER),
        trusted_executors=(EXECUTOR,),
    )
    admin_ctx = context(ADMIN, policy)
    approver_ctx = context(APPROVER, policy)
    other_admin_ctx = context(OTHER_ADMIN, policy)
    async with DatabaseResources(config) as databases:
        launch_repository = SQLiteCampaignLaunchRepository(databases.business_sessions)
        coupon_adapter = InMemoryCouponBatchAdapter(
            materialize_status=coupon_status,
            idempotent_compensation_contract_verified=compensation_contract_verified,
            clock=clock,
        )
        recruitment_adapter = InMemoryRecruitmentAdapter(
            status=recruitment_status,
            clock=clock,
        )
        service = DefaultCampaignLaunchService(
            SQLiteCampaignDraftRepository(databases.business_sessions),
            launches=launch_repository,
            approvals=ApprovalService(
                SQLiteApprovalRepository(databases.platform_sessions),
                policy,
                clock=clock,
                id_factory=lambda: "approval-launch-1",
            ),
            ledger=ExecutionLedger(databases.business_sessions, clock=clock),
            coupon_adapter=coupon_adapter,
            recruitment_adapter=recruitment_adapter,
            compensation_policies=CompensationPolicyRegistry(
                verified_idempotent_versions=(
                    frozenset({"compensation-v1"}) if verified_compensation_policy else frozenset()
                )
            ),
            clock=clock,
            rule_ref_id_factory=lambda: "rule-ref-1",
            id_factory=ids,
        )
        draft = await service.persist_campaign_draft(
            CampaignDraftSpec(
                campaign_id="campaign-1",
                coupon_batch_id="coupon-1",
                recruitment_publication_id="publication-1",
                material_version="material-v1",
                compensation_policy_version="compensation-v1",
            ),
            snapshot(),
            admin_ctx,  # type: ignore[arg-type]
        )
        materialize_args = MaterializeCouponBatchArgs(
            campaign_id=draft.campaign.campaign_id,
            coupon_batch_id=draft.coupon_batch.coupon_batch_id,
            coupon_spec_hash=draft.coupon_batch.coupon_spec_hash,
        )
        publish_args = PublishRecruitmentArgs(
            campaign_id=draft.campaign.campaign_id,
            recruitment_publication_id=(draft.recruitment_publication.recruitment_publication_id),
            merchant_scope_hash=draft.recruitment_publication.merchant_scope_hash,
            material_version=draft.recruitment_publication.material_version,
        )
        binding = await service.request_launch_approval(
            draft=draft,
            materialize_args=materialize_args,
            publish_args=publish_args,
            checkpoint_id="checkpoint-launch-1",
            expires_at=NOW + timedelta(hours=2),
            ctx=admin_ctx,  # type: ignore[arg-type]
        )
        if approve:
            await service.decide_launch_approval(
                approval_id=binding.approval.approval_id,
                decision="approve",
                reason=None,
                ctx=approver_ctx,  # type: ignore[arg-type]
            )
        request = LaunchExecutionRequest(
            plan=binding.plan,
            approval_id=binding.approval.approval_id,
            checkpoint_id=binding.approval.checkpoint_id,
            materialize_args=materialize_args,
            publish_args=publish_args,
        )
        yield LaunchHarness(
            databases=databases,
            service=service,
            launch_repository=launch_repository,
            coupon_adapter=coupon_adapter,
            recruitment_adapter=recruitment_adapter,
            policy=policy,
            admin_ctx=admin_ctx,
            approver_ctx=approver_ctx,
            other_admin_ctx=other_admin_ctx,
            draft=draft,
            binding=binding,
            request=request,
        )
