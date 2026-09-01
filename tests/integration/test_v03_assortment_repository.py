"""SQLite persistence contract for the T06 assortment repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from oria.config import resolve_runtime_config
from oria.data import initialize_data
from oria.domain.business import (
    AssortmentSubmission,
    ConsumerPlacement,
    MerchantNotification,
    SelectionDecision,
)
from oria.domain.product_eligibility import (
    EnrollmentEligibilityAttestation,
    ProductEligibilityCriteria,
)
from oria.storage.assortment import SQLiteAssortmentWorkflowRepository
from oria.storage.database import DatabaseResources
from oria.storage.repositories import BusinessRepositoryError

pytestmark = pytest.mark.integration

_TENANT = "local-community"
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_RULE_HASH = f"sha256:{'a' * 64}"
_PRODUCT_CRITERIA = ProductEligibilityCriteria(
    rule_snapshot_id="rs_123456789012345678901234",
    rule_snapshot_hash=_RULE_HASH,
    policy_ref="product-policy-a",
    policy_version="v1",
    price_min="1",
    price_max="100",
    categories=("food",),
    keywords=("summer",),
)


async def _seed_submission_prerequisites(databases: DatabaseResources) -> None:
    attestation = EnrollmentEligibilityAttestation(
        campaign_id="campaign-a",
        rule_snapshot_ref_id="rule-ref",
        rule_snapshot_hash=_RULE_HASH,
        product_policy_ref="product-policy-a",
        product_policy_version="v1",
        catalog_snapshot_id="catalog-a",
        merchant_criteria_hash=f"sha256:{'b' * 64}",
        product_criteria_hash=f"sha256:{'c' * 64}",
        item_business_keys_hash=f"sha256:{'d' * 64}",
    )
    product_attributes = {
        "captured_at": _NOW.isoformat(),
        "category": "food",
        "currency": "CNY",
        "eligibility_attestation": attestation.model_dump(mode="json"),
        "eligibility_facts": {"available": True, "status": "available"},
        "keyword_labels": ["summer"],
        "normalized_price": "10",
        "normalized_title": "synthetic product",
        "sellability_snapshot": {
            "available": True,
            "catalog_snapshot_id": "catalog-a",
            "product_version": "v1",
            "status": "available",
        },
        "source_ref_hash": f"sha256:{'e' * 64}",
    }
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
                "1, :now, :now, 'demo-m001', 'product-a', 'v1', 'catalog-a', :attributes)"
            ),
            {
                "tenant": _TENANT,
                "now": _NOW,
                "attributes": json.dumps(product_attributes, sort_keys=True),
            },
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
                "INSERT INTO coupon_batches (tenant_id, coupon_batch_id, version, created_at, "
                "updated_at, campaign_id, coupon_spec_hash, status) VALUES (:tenant, "
                "'coupon-a', 1, :now, :now, 'campaign-a', :coupon_hash, 'ready')"
            ),
            {"tenant": _TENANT, "now": _NOW, "coupon_hash": f"sha256:{'f' * 64}"},
        )
        await session.execute(
            text(
                "INSERT INTO campaign_approval_bindings (tenant_id, campaign_id, "
                "enrollment_version, link_version, selection_version, rule_snapshot_hash) "
                "VALUES (:tenant, 'campaign-a', 1, 1, 'pending', :rule_hash)"
            ),
            {"tenant": _TENANT, "rule_hash": _RULE_HASH},
        )
        await session.execute(
            text(
                "INSERT INTO enrollment_coupon_links (tenant_id, enrollment_coupon_link_id, "
                "version, created_at, updated_at, enrollment_item_id, coupon_batch_id, "
                "benefit_tier, status) VALUES (:tenant, 'link-a', 1, :now, :now, 'item-a', "
                "'coupon-a', 'base', 'active')"
            ),
            {"tenant": _TENANT, "now": _NOW},
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


def _decision(
    *, item_id: str = "item-a", selection_version: str = "selection-v1"
) -> SelectionDecision:
    return SelectionDecision(
        tenant_id=_TENANT,
        selection_decision_id=f"decision-{selection_version}-{item_id}",
        campaign_id="campaign-a",
        submission_version="selection-input-v1",
        selection_version=selection_version,
        enrollment_item_id=item_id,
        decision="selected",
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
        binding = await repository.get_approval_binding(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
        )
        assert binding is not None
        candidate_set = await repository.load_submission_candidates(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            rule_snapshot_ref_id="rule-ref",
            product_criteria=_PRODUCT_CRITERIA,
            assortment_policy_ref="policy-a",
            assortment_policy_version="v1",
            approval_binding=binding,
        )
        assert candidate_set.enrollment_item_ids == ("item-a",)

        with pytest.raises(BusinessRepositoryError, match="candidate set changed"):
            async with databases.business_sessions.begin() as session:
                await repository.persist_submission_outcome(
                    session,
                    submission=_submission("selection-input-forged"),
                    enrollment_item_ids=("item-not-a-candidate",),
                    candidate_set=candidate_set,
                    product_criteria=_PRODUCT_CRITERIA,
                    expected_campaign_version=1,
                    outcome="succeeded",
                )

        async with databases.business_sessions.begin() as session:
            await repository.persist_submission_outcome(
                session,
                submission=_submission("selection-input-v1"),
                enrollment_item_ids=("item-a",),
                candidate_set=candidate_set,
                product_criteria=_PRODUCT_CRITERIA,
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
                    candidate_set=candidate_set,
                    product_criteria=_PRODUCT_CRITERIA,
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


@pytest.mark.asyncio
async def test_database_rejects_cross_campaign_submission_membership(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)

        with pytest.raises(IntegrityError):
            async with databases.business_sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO campaigns (tenant_id, campaign_id, version, created_at, "
                        "updated_at, rule_snapshot_ref_id, enrollment_mode, status) VALUES "
                        "(:tenant, 'campaign-b', 1, :now, :now, 'rule-ref', 'hybrid', "
                        "'selecting')"
                    ),
                    {"tenant": _TENANT, "now": _NOW},
                )
                await session.execute(
                    text(
                        "INSERT INTO assortment_submissions (tenant_id, "
                        "assortment_submission_id, version, created_at, updated_at, campaign_id, "
                        "submission_version, assortment_policy_ref, assortment_policy_version, "
                        "status) VALUES (:tenant, 'submission-b', 1, :now, :now, 'campaign-b', "
                        "'submission-b-v1', 'policy-a', 'v1', 'submitted')"
                    ),
                    {"tenant": _TENANT, "now": _NOW},
                )
                await session.execute(
                    text(
                        "INSERT INTO assortment_submission_items (tenant_id, campaign_id, "
                        "submission_version, enrollment_item_id, created_at) VALUES (:tenant, "
                        "'campaign-b', 'submission-b-v1', 'item-a', :now)"
                    ),
                    {"tenant": _TENANT, "now": _NOW},
                )

        async with databases.business_sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM campaigns WHERE campaign_id = 'campaign-b'")
                )
                == 0
            )


@pytest.mark.asyncio
async def test_selection_completion_requires_exact_membership_and_seals_hash_atomically(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)
        binding = await repository.get_approval_binding(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
        )
        assert binding is not None
        candidate_set = await repository.load_submission_candidates(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            rule_snapshot_ref_id="rule-ref",
            product_criteria=_PRODUCT_CRITERIA,
            assortment_policy_ref="policy-a",
            assortment_policy_version="v1",
            approval_binding=binding,
        )
        async with databases.business_sessions.begin() as session:
            await repository.persist_submission_outcome(
                session,
                submission=_submission("selection-input-v1"),
                enrollment_item_ids=("item-a",),
                candidate_set=candidate_set,
                product_criteria=_PRODUCT_CRITERIA,
                expected_campaign_version=1,
                outcome="succeeded",
            )

        with pytest.raises(BusinessRepositoryError, match="requires item decisions"):
            await repository.selection_completion_hash(
                tenant_id=_TENANT,
                campaign_id="campaign-a",
                submission_version="selection-input-v1",
                selection_version="selection-v1",
            )
        with pytest.raises(BusinessRepositoryError, match="outside the submission"):
            async with databases.business_sessions.begin() as session:
                await repository.record_selection_decision(
                    session,
                    decision=_decision(item_id="item-forged"),
                )

        async with databases.business_sessions.begin() as session:
            await repository.record_selection_decision(session, decision=_decision())
        selection_hash = await repository.selection_completion_hash(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            submission_version="selection-input-v1",
            selection_version="selection-v1",
        )
        updated_binding = binding.model_copy(
            update={"selection_version": "selection-v1", "selection_hash": selection_hash}
        )
        async with databases.business_sessions.begin() as session:
            await repository.complete_selection(
                session,
                tenant_id=_TENANT,
                campaign_id="campaign-a",
                submission_version="selection-input-v1",
                selection_version="selection-v1",
                expected_binding=binding,
                updated_binding=updated_binding,
                updated_at=_NOW,
            )

        loaded, _ = await repository.load_submission(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            submission_version="selection-input-v1",
        )
        assert loaded.status == "completed"
        assert loaded.selection_version == "selection-v1"
        assert loaded.selection_hash == selection_hash
        assert (
            await repository.get_approval_binding(
                tenant_id=_TENANT,
                campaign_id="campaign-a",
            )
            == updated_binding
        )
        with pytest.raises(BusinessRepositoryError, match="not accepting decisions"):
            async with databases.business_sessions.begin() as session:
                await repository.record_selection_decision(session, decision=_decision())

        async with databases.business_sessions() as session:
            campaign_status = await session.scalar(
                text("SELECT status FROM campaigns WHERE campaign_id = 'campaign-a'")
            )
            seals = (
                await session.execute(
                    text(
                        "SELECT selection_version, selection_hash FROM assortment_submissions "
                        "WHERE submission_version = 'selection-input-v1'"
                    )
                )
            ).one()
        assert campaign_status == "pending_consumer_publish"
        assert seals == ("selection-v1", selection_hash)


@pytest.mark.asyncio
async def test_selection_completion_rejects_cross_version_decisions(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)
        binding = await repository.get_approval_binding(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
        )
        assert binding is not None
        candidate_set = await repository.load_submission_candidates(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            rule_snapshot_ref_id="rule-ref",
            product_criteria=_PRODUCT_CRITERIA,
            assortment_policy_ref="policy-a",
            assortment_policy_version="v1",
            approval_binding=binding,
        )
        async with databases.business_sessions.begin() as session:
            await repository.persist_submission_outcome(
                session,
                submission=_submission("selection-input-v1"),
                enrollment_item_ids=("item-a",),
                candidate_set=candidate_set,
                product_criteria=_PRODUCT_CRITERIA,
                expected_campaign_version=1,
                outcome="succeeded",
            )
            await repository.record_selection_decision(session, decision=_decision())
            await repository.record_selection_decision(
                session,
                decision=_decision(selection_version="selection-v2"),
            )

        with pytest.raises(BusinessRepositoryError, match="cross-version"):
            await repository.selection_completion_hash(
                tenant_id=_TENANT,
                campaign_id="campaign-a",
                submission_version="selection-input-v1",
                selection_version="selection-v2",
            )
        loaded, _ = await repository.load_submission(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
            submission_version="selection-input-v1",
        )
        assert loaded.status == "submitted"


@pytest.mark.asyncio
async def test_narrow_outcome_projections_only_persist_failed_or_unknown_aggregates(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)
        failed_submission = _submission("projection-failed").model_copy(update={"status": "failed"})
        unknown_placement = ConsumerPlacement(
            tenant_id=_TENANT,
            consumer_placement_id="placement-unknown",
            campaign_id="campaign-a",
            selection_version="selection-v1",
            placement_spec_hash=f"sha256:{'9' * 64}",
            status="unknown",
            request_id="execution-placement",
            version=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
        failed_notification = MerchantNotification(
            tenant_id=_TENANT,
            merchant_notification_id="notification-failed",
            merchant_id="demo-m001",
            campaign_id="campaign-a",
            result_version="selection-v1",
            template_id="selection-result-v1",
            channel="mock-im",
            status="dead_letter",
            attempt_count=1,
            version=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
        projections = (
            repository.submission_outcome_projection(
                execution_id="execution-submission",
                submission=failed_submission,
            ),
            repository.placement_outcome_projection(
                execution_id="execution-placement",
                placement=unknown_placement,
            ),
            repository.notification_outcome_projection(
                execution_id="execution-notification",
                notification=failed_notification,
                outcome="failed",
            ),
        )
        with pytest.raises(BusinessRepositoryError, match="requires a successful execution"):
            async with databases.business_sessions.begin() as session:
                await repository.persist_notification_outcome(
                    session,
                    notification=failed_notification,
                )
        async with databases.business_sessions.begin() as session:
            for projection in projections:
                await projection.apply(session)
        async with databases.business_sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT (SELECT status FROM assortment_submissions WHERE "
                        "submission_version = 'projection-failed'), "
                        "(SELECT status FROM consumer_placements WHERE consumer_placement_id = "
                        "'placement-unknown'), (SELECT status FROM merchant_notifications WHERE "
                        "merchant_notification_id = 'notification-failed'), "
                        "(SELECT COUNT(*) FROM assortment_submission_items WHERE "
                        "submission_version = 'projection-failed')"
                    )
                )
            ).one()

        with pytest.raises(ValueError, match="cannot contain a success state"):
            repository.submission_outcome_projection(
                execution_id="execution-success",
                submission=_submission("projection-success"),
            )

    assert tuple(projection.outcome for projection in projections) == (
        "failed",
        "unknown",
        "failed",
    )
    assert rows == ("failed", "unknown", "dead_letter", 0)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE enrollment_items SET status = 'pending_confirmation' WHERE "
        "enrollment_item_id = 'item-a'",
        "UPDATE enrollment_coupon_links SET status = 'invalid' WHERE enrollment_item_id = 'item-a'",
        "UPDATE product_snapshots SET attributes_json = '{}' WHERE product_snapshot_id = "
        "'product-snapshot-a'",
    ],
)
@pytest.mark.asyncio
async def test_server_candidates_require_confirmation_active_link_and_frozen_attestation(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)
        binding = await repository.get_approval_binding(
            tenant_id=_TENANT,
            campaign_id="campaign-a",
        )
        assert binding is not None
        async with databases.business_sessions.begin() as session:
            await session.execute(text(mutation))

        with pytest.raises(BusinessRepositoryError):
            await repository.load_submission_candidates(
                tenant_id=_TENANT,
                campaign_id="campaign-a",
                rule_snapshot_ref_id="rule-ref",
                product_criteria=_PRODUCT_CRITERIA,
                assortment_policy_ref="policy-a",
                assortment_policy_version="v1",
                approval_binding=binding,
            )
