"""Selection publication integrity checks for the T06 assortment workflow."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oria.core.approvals import ApprovalBusinessBinding
from oria.domain.assortment import (
    AssortmentSelection,
    _require_publishable_selection,
    selection_result_hash,
)
from oria.domain.business import (
    AssortmentSubmission,
    Campaign,
    EnrollmentCouponLink,
    EnrollmentItem,
    SelectionDecision,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, tzinfo=UTC)
TENANT = "tenant-a"


def _decision(*, item_id: str = "item-a", version: str = "selection-v1") -> SelectionDecision:
    return SelectionDecision(
        tenant_id=TENANT,
        selection_decision_id=f"decision-{version}-{item_id}",
        campaign_id="campaign-a",
        submission_version="submission-v1",
        selection_version=version,
        enrollment_item_id=item_id,
        decision="selected",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _selection() -> AssortmentSelection:
    decisions = (_decision(),)
    selection_hash = selection_result_hash(
        campaign_id="campaign-a",
        submission_version="submission-v1",
        selection_version="selection-v1",
        decisions=decisions,
    )
    return AssortmentSelection(
        campaign=Campaign(
            tenant_id=TENANT,
            campaign_id="campaign-a",
            rule_snapshot_ref_id="rule-ref",
            enrollment_mode="hybrid",
            status="pending_consumer_publish",
            version=2,
            created_at=NOW,
            updated_at=NOW,
        ),
        binding=ApprovalBusinessBinding(
            campaign_id="campaign-a",
            enrollment_version=1,
            link_version=1,
            selection_version="selection-v1",
            selection_hash=selection_hash,
            rule_snapshot_hash=f"sha256:{'a' * 64}",
        ),
        submission=AssortmentSubmission(
            tenant_id=TENANT,
            assortment_submission_id="submission-a",
            campaign_id="campaign-a",
            submission_version="submission-v1",
            assortment_policy_ref="policy-a",
            assortment_policy_version="v1",
            status="completed",
            selection_version="selection-v1",
            selection_hash=selection_hash,
            version=2,
            created_at=NOW,
            updated_at=NOW,
        ),
        enrollment_item_ids=("item-a",),
        decisions=decisions,
        items=(
            EnrollmentItem(
                tenant_id=TENANT,
                enrollment_item_id="item-a",
                enrollment_id="enrollment-a",
                campaign_id="campaign-a",
                merchant_id="merchant-a",
                product_ref="product-a",
                product_version="v1",
                product_snapshot_id="snapshot-a",
                mode="hybrid",
                sources=frozenset({"auto"}),
                status="confirmed",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
        links=(
            EnrollmentCouponLink(
                tenant_id=TENANT,
                enrollment_coupon_link_id="link-a",
                enrollment_item_id="item-a",
                coupon_batch_id="coupon-a",
                benefit_tier="base",
                status="active",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
    )


def test_publishable_selection_requires_nonempty_exact_unique_single_version_decisions() -> None:
    selection = _selection()

    assert _require_publishable_selection(selection) == ("item-a",)
    with pytest.raises(ValueError, match="no decisions"):
        _require_publishable_selection(selection.model_copy(update={"decisions": ()}))
    with pytest.raises(ValueError, match="duplicate"):
        _require_publishable_selection(
            selection.model_copy(update={"decisions": selection.decisions * 2})
        )
    with pytest.raises(PermissionError, match="stale"):
        _require_publishable_selection(
            selection.model_copy(update={"decisions": (_decision(version="selection-v2"),)})
        )
    with pytest.raises(ValueError, match="submitted item set"):
        _require_publishable_selection(
            selection.model_copy(update={"enrollment_item_ids": ("item-a", "item-b")})
        )
