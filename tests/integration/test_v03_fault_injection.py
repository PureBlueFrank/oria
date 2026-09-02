"""V0.3-T08 crash-window and partial-success fault injection evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from tests.integration.test_v03_assortment_repository import _seed_submission_prerequisites
from tests.support.launch import NOW as LAUNCH_NOW
from tests.support.launch import launch_harness

from oria.config import resolve_runtime_config
from oria.core.approvals import ApprovalService
from oria.core.execution_ledger import ExecutionLedger
from oria.data import initialize_data
from oria.domain.assortment import (
    PublishConsumerPlacementArgs,
    SendMerchantNotificationArgs,
    SubmitAssortmentArgs,
)
from oria.domain.business import ConsumerPlacement, MerchantNotification
from oria.domain.launch import DefaultCampaignLaunchService
from oria.domain.ledger import Receipt, ToolExecution
from oria.storage.assortment import SQLiteAssortmentWorkflowRepository
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import (
    SQLiteCampaignDraftRepository,
    SQLiteCampaignLaunchRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.recovery]

TENANT = "local-community"
NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
HASH = f"sha256:{'a' * 64}"


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.mark.asyncio
async def test_approved_launch_survives_process_rebuild_before_first_side_effect(
    tmp_path: Path,
) -> None:
    """A persisted approval is sufficient after the approving process disappears."""

    async with launch_harness(tmp_path) as harness:
        ids = count(1)
        restarted = DefaultCampaignLaunchService(
            SQLiteCampaignDraftRepository(harness.databases.business_sessions),
            launches=SQLiteCampaignLaunchRepository(harness.databases.business_sessions),
            approvals=ApprovalService(
                SQLiteApprovalRepository(harness.databases.platform_sessions),
                harness.policy,
                clock=lambda: LAUNCH_NOW + timedelta(minutes=30),
            ),
            ledger=ExecutionLedger(
                harness.databases.business_sessions,
                clock=lambda: LAUNCH_NOW + timedelta(minutes=30),
            ),
            coupon_adapter=harness.coupon_adapter,
            recruitment_adapter=harness.recruitment_adapter,
            clock=lambda: LAUNCH_NOW + timedelta(minutes=30),
            rule_ref_id_factory=lambda: "unused-rule-ref",
            id_factory=lambda prefix: f"{prefix}-after-restart-{next(ids)}",
        )

        result = await restarted.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        replay = await restarted.execute_launch(
            harness.request,
            harness.admin_ctx,  # type: ignore[arg-type]
        )
        async with harness.databases.business_sessions() as session:
            executions = (
                await session.execute(
                    text(
                        "SELECT tool_name, status, COUNT(*) FROM tool_executions "
                        "GROUP BY tool_name, status ORDER BY tool_name"
                    )
                )
            ).all()

    assert result.saga.status == replay.saga.status == "completed"
    assert len(harness.coupon_adapter.materialize_calls) == 1
    assert len(harness.recruitment_adapter.publish_calls) == 1
    assert executions == [
        ("materialize_coupon_batch", "succeeded", 1),
        ("publish_recruitment", "succeeded", 1),
    ]


@pytest.mark.parametrize(
    ("tool_name", "schema", "args", "stable_business_id"),
    [
        (
            "submit_assortment",
            SubmitAssortmentArgs,
            SubmitAssortmentArgs(
                campaign_id="campaign-a",
                enrollment_item_ids=("item-a",),
                assortment_policy_ref="policy-a",
                assortment_policy_version="v1",
                idempotency_key="request-submit-a",
            ).model_dump(),
            "campaign-a:submission-a",
        ),
        (
            "publish_consumer_placement",
            PublishConsumerPlacementArgs,
            PublishConsumerPlacementArgs(
                campaign_id="campaign-a",
                selection_version="selection-v1",
                placement_spec={"channel": "fixture"},
                idempotency_key="request-placement-a",
            ).model_dump(),
            "campaign-a:selection-v1:placement-a",
        ),
    ],
)
@pytest.mark.asyncio
async def test_process_exit_after_external_acceptance_never_blindly_reinvokes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    schema: type[BaseModel],
    args: Mapping[str, Any],
    stable_business_id: str,
) -> None:
    """The accepted-but-not-recorded window converges to unknown, never a retry."""

    config = resolve_runtime_config(environ={}, data_dir=tmp_path / tool_name)
    await initialize_data(config)
    clock = _Clock()
    external_calls: list[str] = []

    async with DatabaseResources(config) as databases:
        ledger = ExecutionLedger(
            databases.business_sessions,
            clock=clock,
            executing_timeout=timedelta(minutes=5),
        )
        reservation = await ledger.reserve_for_args(
            execution_id=f"execution-{tool_name}",
            tenant_id=TENANT,
            tool_name=tool_name,
            tool_schema_version=1,
            schema=schema,
            args=args,
            stable_business_id=stable_business_id,
            checkpoint_id=f"checkpoint-{tool_name}",
        )

        async def invoke(idempotency_key: str) -> Receipt:
            external_calls.append(idempotency_key)
            return Receipt(
                receipt_id=f"receipt-{tool_name}",
                adapter_id=f"mock-{tool_name}",
                resource_ref=f"campaign:{stable_business_id}",
                status="accepted",
                received_at=clock(),
                summary_hash=HASH,
            )

        async def exit_before_local_commit(*_: object, **__: object) -> ToolExecution:
            raise SystemExit("injected process exit after external acceptance")

        monkeypatch.setattr(ledger, "record_success", exit_before_local_commit)
        with pytest.raises(SystemExit, match="injected process exit"):
            await ledger.execute(reservation, invoke)

        restarted = ExecutionLedger(
            databases.business_sessions,
            clock=clock,
            executing_timeout=timedelta(minutes=5),
        )
        replay = await restarted.reserve_for_args(
            execution_id=f"execution-{tool_name}-retry",
            tenant_id=TENANT,
            tool_name=tool_name,
            tool_schema_version=1,
            schema=schema,
            args=args,
            stable_business_id=stable_business_id,
            checkpoint_id=f"checkpoint-{tool_name}",
        )
        waiting = await restarted.recover_stale_executing(replay)
        clock.advance(timedelta(minutes=6))
        unknown = await restarted.recover_stale_executing(waiting)
        terminal_replay = await restarted.reserve_for_args(
            execution_id=f"execution-{tool_name}-late-retry",
            tenant_id=TENANT,
            tool_name=tool_name,
            tool_schema_version=1,
            schema=schema,
            args=args,
            stable_business_id=stable_business_id,
            checkpoint_id=f"checkpoint-{tool_name}",
        )
        with pytest.raises(ValueError, match="reserved ledger value"):
            await restarted.execute(terminal_replay, invoke)
        async with databases.business_sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT status, compensation_status, attempt_count, COUNT(*) "
                        "FROM tool_executions GROUP BY status, compensation_status, attempt_count"
                    )
                )
            ).all()

    assert waiting.status == "executing"
    assert unknown.status == terminal_replay.status == "unknown"
    assert rows == [("unknown", "reconciliation_required", 1, 1)]
    assert len(external_calls) == 1


@pytest.mark.asyncio
async def test_partial_notification_failure_does_not_roll_back_consumer_placement(
    tmp_path: Path,
) -> None:
    """One dead letter remains isolated from an already published placement and sent peer."""

    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "notification-partial")
    await initialize_data(config)
    adapter_calls: list[str] = []

    async with DatabaseResources(config) as databases:
        await _seed_submission_prerequisites(databases)
        repository = SQLiteAssortmentWorkflowRepository(databases.business_sessions)
        placement = ConsumerPlacement(
            tenant_id=TENANT,
            consumer_placement_id="placement-published",
            campaign_id="campaign-a",
            selection_version="selection-v1",
            placement_spec_hash=HASH,
            status="published",
            request_id="placement-request",
            receipt_id="placement-receipt",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        async with databases.business_sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO consumer_placements (tenant_id, consumer_placement_id, version, "
                    "created_at, updated_at, campaign_id, selection_version, placement_spec_hash, "
                    "status, request_id, receipt_id) VALUES (:tenant_id, :placement_id, 1, :now, "
                    ":now, 'campaign-a', 'selection-v1', :spec_hash, 'published', "
                    "'placement-request', 'placement-receipt')"
                ),
                {
                    "tenant_id": TENANT,
                    "placement_id": placement.consumer_placement_id,
                    "now": NOW,
                    "spec_hash": HASH,
                },
            )

        ledger = ExecutionLedger(databases.business_sessions, clock=lambda: NOW)
        outcomes = (("merchant-sent", "accepted"), ("merchant-dead", "rejected"))
        for merchant_key, receipt_status in outcomes:
            request = SendMerchantNotificationArgs(
                merchant_id="demo-m001",
                campaign_id="campaign-a",
                result_version=f"selection-v1-{merchant_key}",
                template_id="selection-result-v1",
                channel="mock-im",
                idempotency_key=f"request-{merchant_key}",
            )
            execution = await ledger.reserve_for_args(
                execution_id=f"execution-{merchant_key}",
                tenant_id=TENANT,
                tool_name="send_merchant_notification",
                tool_schema_version=1,
                schema=SendMerchantNotificationArgs,
                args=request.model_dump(),
                stable_business_id=merchant_key,
                checkpoint_id="checkpoint-notification",
            )
            notification = MerchantNotification(
                tenant_id=TENANT,
                merchant_notification_id=f"notification-{merchant_key}",
                merchant_id="demo-m001",
                campaign_id="campaign-a",
                result_version=request.result_version,
                template_id=request.template_id,
                channel=request.channel,
                status="sent" if receipt_status == "accepted" else "dead_letter",
                attempt_count=1,
                receipt_id=None,
                version=1,
                created_at=NOW,
                updated_at=NOW,
            )

            async def invoke(
                idempotency_key: str,
                *,
                status: str = receipt_status,
                key: str = merchant_key,
            ) -> Receipt:
                adapter_calls.append(idempotency_key)
                return Receipt(
                    receipt_id=f"receipt-{key}",
                    adapter_id="mock-notification",
                    resource_ref=f"merchant_notification:{key}",
                    status=status,  # type: ignore[arg-type]
                    received_at=NOW,
                    summary_hash=HASH,
                )

            async def success_write(
                session: Any,
                *,
                value: MerchantNotification = notification,
            ) -> None:
                await repository.persist_notification_outcome(
                    session,
                    notification=value,
                )

            projection = repository.notification_outcome_projection
            execution_id = execution.execution_id

            def failed_projection(
                outcome: Any,
                execution_id: str = execution_id,
                value: MerchantNotification = notification,
                projection: Any = projection,
            ) -> Any:
                return projection(
                    execution_id=execution_id,
                    notification=value,
                    outcome=outcome,
                )

            await ledger.execute(
                execution,
                invoke,
                business_write=success_write,
                outcome_projection=failed_projection,
            )

        async with databases.business_sessions() as session:
            placement_status = await session.scalar(
                text(
                    "SELECT status FROM consumer_placements WHERE consumer_placement_id = "
                    "'placement-published'"
                )
            )
            notifications = (
                await session.execute(
                    text(
                        "SELECT status, COUNT(*) FROM merchant_notifications "
                        "GROUP BY status ORDER BY status"
                    )
                )
            ).all()
            executions = (
                await session.execute(
                    text(
                        "SELECT status, COUNT(*) FROM tool_executions "
                        "GROUP BY status ORDER BY status"
                    )
                )
            ).all()

    assert placement_status == "published"
    assert notifications == [("dead_letter", 1), ("sent", 1)]
    assert executions == [("failed", 1), ("succeeded", 1)]
    assert len(adapter_calls) == 2
