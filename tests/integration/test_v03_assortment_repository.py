"""SQLite persistence contract for the T06 assortment repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.data import initialize_data
from oria.domain.business import AssortmentSubmission
from oria.storage.assortment import SQLiteAssortmentWorkflowRepository
from oria.storage.database import DatabaseResources

pytestmark = pytest.mark.integration

_TENANT = "local-community"
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_RULE_HASH = f"sha256:{'a' * 64}"


async def _seed_submission_prerequisites(databases: DatabaseResources) -> None:
    async with databases.business_sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO campaign_rule_snapshot_refs (tenant_id, "
                "campaign_rule_snapshot_ref_id, version, created_at, updated_at, snapshot_id, "
                "snapshot_hash) VALUES (:tenant, 'rule-ref', 1, :now, :now, "
                "'rs_123456789012345678901234', :rule_hash)"
            ),
            {"tenant": _TENANT, "now": _NOW, "rule_hash": _RULE_HASH},
        )
        await session.execute(
            text(
                "INSERT INTO campaigns (tenant_id, campaign_id, version, created_at, updated_at, "
                "rule_snapshot_ref_id, enrollment_mode, status) VALUES (:tenant, 'campaign-a', "
                "1, :now, :now, 'rule-ref', 'hybrid', 'selecting')"
            ),
            {"tenant": _TENANT, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO product_snapshots (tenant_id, product_snapshot_id, version, "
                "created_at, updated_at, merchant_id, product_ref, product_version, "
                "catalog_snapshot_id, attributes_json) VALUES (:tenant, 'product-snapshot-a', "
                "1, :now, :now, 'demo-m001', 'product-a', 'v1', 'catalog-a', '{}')"
            ),
            {"tenant": _TENANT, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO enrollments (tenant_id, enrollment_id, version, created_at, "
                "updated_at, campaign_id, merchant_id, mode, status) VALUES (:tenant, "
                "'enrollment-a', 1, :now, :now, 'campaign-a', 'demo-m001', 'hybrid', 'closed')"
            ),
            {"tenant": _TENANT, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO enrollment_items (tenant_id, enrollment_item_id, version, "
                "created_at, updated_at, enrollment_id, campaign_id, merchant_id, product_ref, "
                "product_version, product_snapshot_id, mode, sources_json, status) VALUES "
                "(:tenant, 'item-a', 1, :now, :now, 'enrollment-a', 'campaign-a', "
                "'demo-m001', 'product-a', 'v1', 'product-snapshot-a', 'hybrid', "
                "'[\"auto\"]', 'confirmed')"
            ),
            {"tenant": _TENANT, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO campaign_approval_bindings (tenant_id, campaign_id, "
                "enrollment_version, link_version, selection_version, rule_snapshot_hash) "
                "VALUES (:tenant, 'campaign-a', 1, 1, 'pending', :rule_hash)"
            ),
            {"tenant": _TENANT, "rule_hash": _RULE_HASH},
        )


def _submission(version: str) -> AssortmentSubmission:
    return AssortmentSubmission(
        tenant_id=_TENANT,
        assortment_submission_id=f"submission-{version}",
        campaign_id="campaign-a",
        submission_version=version,
        assortment_policy_ref="policy-a",
        assortment_policy_version="v1",
        status="submitted",
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_submission_and_membership_commit_atomically_and_rollback_together(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)

        async with databases.business_sessions.begin() as session:
            await repository.persist_submission_outcome(
                session,
                submission=_submission("selection-input-v1"),
                enrollment_item_ids=("item-a",),
                expected_campaign_version=1,
                outcome="succeeded",
            )

        loaded, item_ids = await repository.load_submission(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            submission_version="selection-input-v1",
        )
        assert loaded.status == "submitted"
        assert item_ids == ("item-a",)

        with pytest.raises(RuntimeError, match="injected transaction failure"):
            async with databases.business_sessions.begin() as session:
                await repository.persist_submission_outcome(
                    session,
                    submission=_submission("selection-input-rollback"),
                    enrollment_item_ids=("item-a",),
                    expected_campaign_version=1,
                    outcome="succeeded",
                )
                raise RuntimeError("injected transaction failure")

        async with databases.business_sessions() as session:
            submissions = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM assortment_submissions WHERE submission_version = "
                    "'selection-input-rollback'"
                )
            )
            memberships = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM assortment_submission_items WHERE submission_version "
                    "= 'selection-input-rollback'"
                )
            )
        assert submissions == memberships == 0
