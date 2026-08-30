"""Unit coverage for validated, immutable local campaign drafts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from oria.core.types import Principal
from oria.domain.launch import CampaignDraftSpec, DefaultCampaignLaunchService
from oria.permission.local import LocalPolicyEngine
from oria.rag.models import CampaignRuleSnapshot
from oria.resources.loader import load_demo_data

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
HASH_A = f"sha256:{'a' * 64}"


class _DraftRepository:
    def __init__(self) -> None:
        self.bundles: list[dict[str, object]] = []

    async def create_bundle(self, **values: object) -> None:
        self.bundles.append(values)


def _principal(subject_id: str, *roles: str) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id="tenant-a",
        kind="human",
        roles=roles,
        authn_method="trusted-test-profile",
    )


ADMIN = _principal("admin-a", "campaign_admin")
EXECUTOR = Principal(
    subject_id="worker-a",
    tenant_id="tenant-a",
    kind="service",
    roles=("runtime",),
    authn_method="trusted-test-profile",
)


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        actor=ADMIN,
        executor=EXECUTOR,
        tenant_id="tenant-a",
        correlation_id="correlation-a",
        policy=LocalPolicyEngine(trusted_actors=(ADMIN,), trusted_executors=(EXECUTOR,)),
    )


def _snapshot() -> CampaignRuleSnapshot:
    rules = load_demo_data().rules
    placeholder = CampaignRuleSnapshot(
        snapshot_id="rs_123456789012345678901234",
        snapshot_hash="sha256:" + "0" * 64,
        tenant_id="tenant-a",
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


def _spec() -> CampaignDraftSpec:
    return CampaignDraftSpec(
        campaign_id="campaign-1",
        coupon_batch_id="coupon-1",
        recruitment_publication_id="publication-1",
        material_version="material-v1",
        compensation_policy_version="compensation-v1",
    )


@pytest.mark.asyncio
async def test_persist_campaign_draft_freezes_every_launch_binding_before_writing() -> None:
    repository = _DraftRepository()
    service = DefaultCampaignLaunchService(
        repository,  # type: ignore[arg-type]
        clock=lambda: NOW,
        rule_ref_id_factory=lambda: "rule-ref-1",
    )

    draft = await service.persist_campaign_draft(_spec(), _snapshot(), _context())  # type: ignore[arg-type]

    assert len(repository.bundles) == 1
    assert draft.campaign.status == "draft"
    assert draft.coupon_batch.status == "draft"
    assert draft.recruitment_publication.status == "pending"
    assert draft.rule_snapshot_ref.snapshot_hash == _snapshot().snapshot_hash
    assert draft.recruitment_publication.merchant_scope_hash.startswith("sha256:")
    assert draft.coupon_batch.coupon_spec_hash.startswith("sha256:")
    assert draft.compensation_policy_version == "compensation-v1"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda snapshot: snapshot.model_copy(update={"snapshot_hash": HASH_A}),
            id="rule-hash",
        ),
        pytest.param(
            lambda snapshot: snapshot.model_copy(
                update={
                    "benefit_policy": snapshot.benefit_policy.model_copy(
                        update={"budget_cap": Decimal("NaN")}
                    )
                }
            ),
            id="amount",
        ),
        pytest.param(
            lambda snapshot: snapshot.model_copy(
                update={
                    "basic": snapshot.basic.model_copy(
                        update={"enrollment_window": ("2026-01-01T00:00:00/2026-01-02T00:00:00")}
                    )
                }
            ),
            id="date",
        ),
        pytest.param(
            lambda snapshot: snapshot.model_copy(
                update={
                    "merchant_material": snapshot.merchant_material.model_copy(
                        update={"hero_image_ref": "https://untrusted.invalid/image.png"}
                    )
                }
            ),
            id="material-ref",
        ),
        pytest.param(
            lambda snapshot: snapshot.model_copy(
                update={
                    "merchant_material": snapshot.merchant_material.model_copy(
                        update={"title": "   ", "introduction": ""}
                    )
                }
            ),
            id="material-text",
        ),
    ],
)
@pytest.mark.asyncio
async def test_persist_campaign_draft_rejects_invalid_rules_without_any_write(
    mutate: object,
) -> None:
    repository = _DraftRepository()
    service = DefaultCampaignLaunchService(repository, clock=lambda: NOW)  # type: ignore[arg-type]
    invalid = mutate(_snapshot())  # type: ignore[operator]

    with pytest.raises(ValueError):
        await service.persist_campaign_draft(_spec(), invalid, _context())  # type: ignore[arg-type]

    assert repository.bundles == []
