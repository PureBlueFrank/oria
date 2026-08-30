"""SQLite contracts for local campaign drafts without external side effects."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.core.types import Principal
from oria.data import initialize_data
from oria.domain.launch import CampaignDraftSpec, DefaultCampaignLaunchService
from oria.permission.local import LocalPolicyEngine
from oria.rag.models import CampaignRuleSnapshot
from oria.resources.loader import load_demo_data
from oria.storage.database import DatabaseResources
from oria.storage.repositories import SQLiteCampaignDraftRepository

pytestmark = pytest.mark.contract

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


def _context() -> SimpleNamespace:
    actor = Principal(
        subject_id="campaign-admin",
        tenant_id="local-community",
        kind="human",
        roles=("campaign_admin",),
        authn_method="trusted-test-profile",
    )
    executor = Principal(
        subject_id="campaign-worker",
        tenant_id="local-community",
        kind="service",
        roles=("runtime",),
        authn_method="trusted-test-profile",
    )
    return SimpleNamespace(
        actor=actor,
        executor=executor,
        tenant_id=actor.tenant_id,
        correlation_id="campaign-draft-correlation",
        policy=LocalPolicyEngine(trusted_actors=(actor,), trusted_executors=(executor,)),
    )


def _snapshot() -> CampaignRuleSnapshot:
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


@pytest.mark.asyncio
async def test_campaign_draft_writes_only_local_business_facts_without_ledger_or_outbox(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    await initialize_data(config)
    ctx = _context()

    async with DatabaseResources(config) as databases:
        service = DefaultCampaignLaunchService(
            SQLiteCampaignDraftRepository(databases.business_sessions),
            clock=lambda: NOW,
            rule_ref_id_factory=lambda: "rule-ref-1",
        )
        draft = await service.persist_campaign_draft(
            CampaignDraftSpec(
                campaign_id="campaign-1",
                coupon_batch_id="coupon-1",
                recruitment_publication_id="publication-1",
                material_version="material-v1",
                compensation_policy_version="compensation-v1",
            ),
            _snapshot(),
            ctx,  # type: ignore[arg-type]
        )

        async with databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM campaign_rule_snapshot_refs), "
                        "(SELECT COUNT(*) FROM campaigns), "
                        "(SELECT COUNT(*) FROM coupon_batches), "
                        "(SELECT COUNT(*) FROM recruitment_publications), "
                        "(SELECT COUNT(*) FROM tool_executions), "
                        "(SELECT COUNT(*) FROM domain_events), "
                        "(SELECT COUNT(*) FROM audit_events), "
                        "(SELECT COUNT(*) FROM outbox)"
                    )
                )
            ).one()

    assert counts == (1, 1, 1, 1, 0, 0, 0, 0)
    assert draft.campaign_draft_hash.startswith("sha256:")
    assert draft.coupon_batch_draft_hash.startswith("sha256:")
