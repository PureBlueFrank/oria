"""Write-before-authorization boundaries for the T06 side-effect services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from oria.adapters.assortment import (
    InMemoryAssortmentAdapter,
    InMemoryConsumerPlacementAdapter,
    InMemoryMerchantNotificationAdapter,
)
from oria.config import resolve_runtime_config
from oria.core.approvals import ApprovalBusinessBinding
from oria.core.execution_ledger import ExecutionEventBundle, ExecutionLedger
from oria.core.types import EventEnvelope, ResourceRef
from oria.data import initialize_data
from oria.domain.assortment import (
    AssortmentSelection,
    AssortmentService,
    MerchantNotificationMessage,
    PublishConsumerPlacementArgs,
    SendMerchantNotificationArgs,
    SubmitAssortmentArgs,
    selection_result_hash,
)
from oria.domain.business import (
    AssortmentSubmission,
    Campaign,
    EnrollmentCouponLink,
    SelectionDecision,
)
from oria.domain.ledger import OutboxRecord, Receipt
from oria.storage.database import DatabaseResources

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
TENANT = "local-community"
HASH = f"sha256:{'a' * 64}"


def _binding(selection_hash: str | None = None) -> ApprovalBusinessBinding:
    return ApprovalBusinessBinding(
        campaign_id="campaign-a",
        enrollment_version=1,
        link_version=1,
        selection_version="selection-v1",
        selection_hash=selection_hash,
        rule_snapshot_hash=HASH,
    )


def _selection() -> AssortmentSelection:
    decision = SelectionDecision(
        tenant_id=TENANT,
        selection_decision_id="decision-a",
        campaign_id="campaign-a",
        submission_version="submission-v1",
        selection_version="selection-v1",
        enrollment_item_id="item-a",
        decision="selected",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    selection_hash = selection_result_hash(
        campaign_id="campaign-a",
        submission_version="submission-v1",
        selection_version="selection-v1",
        decisions=(decision,),
    )
    return AssortmentSelection.model_construct(
        campaign=Campaign(
            tenant_id=TENANT,
            campaign_id="campaign-a",
            rule_snapshot_ref_id="rule-a",
            enrollment_mode="hybrid",
            status="pending_consumer_publish",
            version=2,
            created_at=NOW,
            updated_at=NOW,
        ),
        binding=_binding(selection_hash),
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
        decisions=(decision,),
        items=(),
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


@pytest.mark.asyncio
async def test_all_t06_precheck_denials_happen_before_reservation(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    assortment_adapter = InMemoryAssortmentAdapter()
    placement_adapter = InMemoryConsumerPlacementAdapter()
    notification_adapter = InMemoryMerchantNotificationAdapter()
    approvals = SimpleNamespace(
        authorize_resume=AsyncMock(side_effect=PermissionError("approval denied"))
    )
    repository = SimpleNamespace(
        load_selection=AsyncMock(return_value=_selection()),
        get_approval_binding=AsyncMock(return_value=_binding()),
        notification_message=AsyncMock(
            return_value=MerchantNotificationMessage(
                merchant_id="merchant-a",
                campaign_id="campaign-a",
                result_version="selection-v1",
                selected_item_ids=("item-a",),
                rejected_reasons=(),
                template_id="selection-result-v1",
                channel="mock-im",
            )
        ),
    )
    context = SimpleNamespace(tenant_id=TENANT, run_id="run-a")

    async with DatabaseResources(config) as databases:
        service = AssortmentService(
            campaigns=SimpleNamespace(),
            rule_refs=SimpleNamespace(),
            rule_snapshots=SimpleNamespace(),
            repository=repository,
            ledger=ExecutionLedger(databases.business_sessions),
            approvals=approvals,
            assortment_adapter=assortment_adapter,
            placement_adapter=placement_adapter,
            notification_adapter=notification_adapter,
            approval_invalidator=SimpleNamespace(),
        )
        service._assortment_precheck = AsyncMock(  # type: ignore[method-assign]
            return_value=(
                SubmitAssortmentArgs(
                    campaign_id="campaign-a",
                    enrollment_item_ids=("item-a",),
                    assortment_policy_ref="policy-a",
                    assortment_policy_version="v1",
                    idempotency_key="submit-request-a",
                ),
                HASH,
                _binding(),
                object(),
                object(),
                object(),
            )
        )

        calls = (
            service.submit(
                SubmitAssortmentArgs(
                    campaign_id="campaign-a",
                    enrollment_item_ids=("item-a",),
                    assortment_policy_ref="policy-a",
                    assortment_policy_version="v1",
                    idempotency_key="submit-request-a",
                ),
                context,
            ),
            service.publish_consumer_placement(
                PublishConsumerPlacementArgs(
                    campaign_id="campaign-a",
                    selection_version="selection-v1",
                    placement_spec={"target": "consumer"},
                    idempotency_key="publish-request-a",
                ),
                context,
            ),
            service.send_merchant_notification(
                SendMerchantNotificationArgs(
                    merchant_id="merchant-a",
                    campaign_id="campaign-a",
                    result_version="selection-v1",
                    template_id="selection-result-v1",
                    channel="mock-im",
                    idempotency_key="notify-request-a",
                ),
                context,
            ),
        )
        for call in calls:
            with pytest.raises(PermissionError, match="approval denied"):
                await call

        async with databases.business_sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM tool_executions), "
                        "(SELECT COUNT(*) FROM audit_events), (SELECT COUNT(*) FROM outbox)"
                    )
                )
            ).one()

    assert counts == (0, 0, 0)
    assert assortment_adapter.calls == []
    assert placement_adapter.calls == []
    assert notification_adapter.calls == []


@pytest.mark.asyncio
async def test_rejected_side_effect_commits_terminal_ledger_audit_and_outbox(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    calls = 0

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(databases.business_sessions)
        reserved = await ledger.reserve_for_args(
            execution_id="execution-rejected",
            tenant_id=TENANT,
            tool_name="submit_assortment",
            tool_schema_version=1,
            schema=SubmitAssortmentArgs,
            args={
                "campaign_id": "campaign-a",
                "enrollment_item_ids": ("item-a",),
                "assortment_policy_ref": "policy-a",
                "assortment_policy_version": "v1",
                "idempotency_key": "[request-key]",
                "approval_id": None,
            },
            stable_business_id="campaign-a:submission-v1",
            checkpoint_id="checkpoint-a",
            request_idempotency_key="submit-request-a",
        )

        async def invoke(_: str) -> Receipt:
            nonlocal calls
            calls += 1
            return Receipt(
                receipt_id="receipt-rejected",
                adapter_id="mock-assortment",
                resource_ref="assortment:campaign-a",
                status="rejected",
                received_at=NOW,
                summary_hash=HASH,
            )

        def events(outcome: str) -> ExecutionEventBundle:
            assert outcome == "failed"
            audit = EventEnvelope(
                event_id="audit-rejected",
                occurred_at=NOW,
                tenant_id=TENANT,
                actor="operator-a",
                action="submit_assortment",
                resource=ResourceRef(
                    resource_type="campaign",
                    resource_id="campaign-a",
                    tenant_id=TENANT,
                ),
                decision="allow",
                policy_version="policy-v1",
                args_hash=reserved.canonical_args_hash,
                result="failure",
                correlation_id="correlation-a",
                payload={"outcome": outcome},
            )
            outbox = OutboxRecord(
                event_id="outbox-rejected",
                tenant_id=TENANT,
                topic="assortment.submission_failed",
                payload_json='{"outcome":"failed"}',
                occurred_at=NOW,
                available_at=NOW + timedelta(seconds=1),
            )
            return ExecutionEventBundle(audit_events=(audit,), outbox_records=(outbox,))

        failed = await ledger.execute(reserved, invoke, outcome_events=events)
        async with databases.business_sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, (SELECT COUNT(*) FROM audit_events), "
                        "(SELECT COUNT(*) FROM outbox) FROM tool_executions "
                        "WHERE execution_id = 'execution-rejected'"
                    )
                )
            ).one()

    assert failed.status == "failed"
    assert row == ("failed", 1, 1)
    assert calls == 1
