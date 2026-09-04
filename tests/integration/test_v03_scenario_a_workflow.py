from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from oria.config import resolve_runtime_config
from oria.orchestrator.local_executor import (
    LocalWorkflowResult,
    campaign_admin,
    close_enrollment_window,
    complete_selection,
    decide_confirmation,
    decide_local_approval,
    inject_merchant_event,
    inject_selection_decision,
    start_local_workflow,
    workflow_database_paths,
)

pytestmark = [pytest.mark.integration, pytest.mark.recovery, pytest.mark.security]


def _interrupt(result: LocalWorkflowResult, kind: str) -> dict[str, Any]:
    matches = [item for item in result.interrupts if item.get("kind") == kind]
    assert len(matches) == 1
    return matches[0]


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, statement: str) -> list[sqlite3.Row]:
    return connection.execute(statement).fetchall()


@pytest.mark.asyncio
async def test_scenario_a_completes_across_process_scoped_runtimes_and_persists_evidence(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    thread_id = "scenario-a-e2e"
    campaign_id = "campaign-scenario-a-e2e"

    result = await start_local_workflow(
        config,
        thread_id=thread_id,
        campaign_id=campaign_id,
        user_request="夏季餐饮联合优惠活动",
    )
    launch = _interrupt(result, "launch_approval")
    launch_approval_id = str(launch["approval_id"])
    assert result.view.current_stage == "招商发布审批"
    assert result.view.merchant_matches.matched_count == 10

    with pytest.raises(PermissionError, match="not authorized"):
        await decide_local_approval(
            config,
            thread_id=thread_id,
            approval_id=launch_approval_id,
            decision="approve",
            reason=None,
            decision_actor=campaign_admin(),
        )

    with pytest.raises(PermissionError, match="active interrupt"):
        await decide_local_approval(
            config,
            thread_id=thread_id,
            approval_id="approval-forged",
            decision="approve",
            reason=None,
        )

    result = await decide_local_approval(
        config,
        thread_id=thread_id,
        approval_id=launch_approval_id,
        decision="approve",
        reason="fixture launch approval",
    )
    _interrupt(result, "enrollment_window")
    assert result.view.coupon_batch is not None
    assert result.view.coupon_batch.status == "ready"
    assert result.view.coupon_batch.quantity is None
    assert result.view.coupon_batch.quantity_note == "未单独配置固定张数"

    merchant = await inject_merchant_event(
        config,
        thread_id=thread_id,
        source_event_id="merchant-event-1",
        merchant_id="demo-m001",
        product_ref="synthetic-product-demo-m001",
    )
    assert merchant.detail["event_status"] == "accepted"
    duplicate = await inject_merchant_event(
        config,
        thread_id=thread_id,
        source_event_id="merchant-event-1",
        merchant_id="demo-m001",
        product_ref="synthetic-product-demo-m001",
    )
    assert duplicate.detail["event_status"] == "duplicate"

    result = await close_enrollment_window(
        config,
        thread_id=thread_id,
        source_event_id="window-close-1",
    )
    confirmed_task_ids: list[str] = []
    while result.interrupts and result.interrupts[0].get("kind") == "business_confirmation":
        task_id = str(result.interrupts[0]["confirmation_task_id"])
        confirmed_task_ids.append(task_id)
        result = await decide_confirmation(
            config,
            thread_id=thread_id,
            confirmation_task_id=task_id,
            decision="confirm",
        )
    assert len(confirmed_task_ids) == 3
    _interrupt(result, "selection_event")
    assert result.view.enrollment_items[0].merchant_id == "demo-m001"
    assert result.view.enrollment_items[0].product_ref == "synthetic-product-demo-m001"

    decision = await inject_selection_decision(
        config,
        thread_id=thread_id,
        source_event_id="selection-decision-1",
        selection_version="selection-v1",
        decision="selected",
        reason_code=None,
    )
    assert decision.detail["selection_version"] == "selection-v1"
    with pytest.raises(PermissionError, match=r"frozen binding|trusted inbox"):
        await inject_selection_decision(
            config,
            thread_id=thread_id,
            source_event_id="selection-decision-1",
            selection_version="selection-v1",
            decision="selected",
            reason_code=None,
        )

    result = await complete_selection(
        config,
        thread_id=thread_id,
        source_event_id="selection-complete-1",
        selection_version="selection-v1",
    )
    consumer = _interrupt(result, "consumer_publish_approval")
    consumer_approval_id = str(consumer["approval_id"])
    assert consumer_approval_id != launch_approval_id
    assert result.view.selection_summary.selected_count == 1
    assert result.view.selection_summary.coupon_linked_count == 1
    assert result.view.selection_summary.selected_products == ("synthetic-product-demo-m001",)
    assert result.view.selection_decisions[0].product_ref == "synthetic-product-demo-m001"
    assert result.view.selection_decisions[0].decision == "selected"
    assert result.view.selection_decisions[0].reason == "selected_by_assortment"
    assert result.view.placement is not None
    assert result.view.placement.channel == "synthetic-home-feed"
    assert "1 个入选商品" in result.view.placement.content_example

    result = await decide_local_approval(
        config,
        thread_id=thread_id,
        approval_id=consumer_approval_id,
        decision="approve",
        reason="fixture consumer publish approval",
    )
    assert result.status == "completed"
    assert not result.interrupts
    assert result.view.terminal_outcome == "completed"
    assert result.view.notification_messages[0].merchant_id == "demo-m001"
    assert "入选商品 synthetic-product-demo-m001" in result.view.notification_messages[0].message

    platform_path, business_path = workflow_database_paths(config)
    with _connect_read_only(business_path) as business:
        campaign = _rows(
            business,
            "SELECT campaign_id, version, status FROM campaigns",
        )
        assert [tuple(row) for row in campaign] == [(campaign_id, 6, "active")]
        assert [
            tuple(row) for row in _rows(business, "SELECT version, status FROM coupon_batches")
        ] == [(3, "ready")]
        assert [
            tuple(row)
            for row in _rows(business, "SELECT version, status FROM recruitment_publications")
        ] == [(2, "published")]
        saga = _rows(
            business,
            "SELECT version, status, checkpoint FROM launch_saga_states",
        )
        assert len(saga) == 1 and tuple(saga[0][:2]) == (4, "completed")
        assert saga[0]["checkpoint"]

        enrollment_items = _rows(
            business,
            "SELECT version, mode, sources_json, status FROM enrollment_items",
        )
        assert len(enrollment_items) == 1
        assert enrollment_items[0]["version"] == 3
        assert enrollment_items[0]["mode"] == "hybrid"
        assert json.loads(enrollment_items[0]["sources_json"]) == ["auto", "merchant"]
        assert enrollment_items[0]["status"] == "confirmed"
        assert (
            _rows(
                business,
                "SELECT campaign_id, merchant_id, product_ref, product_version, COUNT(*) "
                "FROM enrollment_items GROUP BY campaign_id, merchant_id, product_ref, "
                "product_version HAVING COUNT(*) > 1",
            )
            == []
        )

        confirmation_tasks = _rows(
            business,
            "SELECT version, sequence, status FROM confirmation_tasks ORDER BY sequence",
        )
        assert [row["sequence"] for row in confirmation_tasks] == [1, 2, 3]
        assert all(row["version"] >= 2 for row in confirmation_tasks)
        assert {row["status"] for row in confirmation_tasks} == {"confirmed"}
        assert [
            tuple(row)
            for row in _rows(business, "SELECT version, status FROM enrollment_coupon_links")
        ] == [(1, "active")]
        assert [
            tuple(row)
            for row in _rows(
                business,
                "SELECT version, status, selection_version FROM assortment_submissions",
            )
        ] == [(2, "completed", "selection-v1")]
        assert [
            tuple(row)
            for row in _rows(
                business,
                "SELECT version, decision, selection_version FROM selection_decisions",
            )
        ] == [(1, "selected", "selection-v1")]
        assert [
            tuple(row) for row in _rows(business, "SELECT version, status FROM consumer_placements")
        ] == [(1, "published")]
        assert [
            tuple(row)
            for row in _rows(
                business, "SELECT version, status, attempt_count FROM merchant_notifications"
            )
        ] == [(1, "sent", 1)]

        executions = _rows(
            business,
            "SELECT tool_name, idempotency_key, status, attempt_count, checkpoint_id "
            "FROM tool_executions",
        )
        assert len(executions) == 13
        assert {row["status"] for row in executions} == {"succeeded"}
        assert {row["attempt_count"] for row in executions} == {1}
        assert all(row["checkpoint_id"] for row in executions)
        assert (
            _rows(
                business,
                "SELECT tenant_id, tool_name, idempotency_key, COUNT(*) FROM tool_executions "
                "GROUP BY tenant_id, tool_name, idempotency_key HAVING COUNT(*) > 1",
            )
            == []
        )
        business_audit = _rows(
            business,
            "SELECT COUNT(*) AS total, COUNT(DISTINCT correlation_id) AS correlations, "
            "MIN(correlation_id) AS correlation_id FROM audit_events",
        )[0]
        assert tuple(business_audit) == (13, 1, f"scenario-a:{thread_id}")
        assert tuple(
            _rows(
                business,
                "SELECT COUNT(*) AS total, COUNT(DISTINCT event_id) AS unique_ids FROM outbox",
            )[0]
        ) == (14, 14)

    with _connect_read_only(platform_path) as platform:
        approvals = _rows(
            platform,
            "SELECT approval_id, approval_action, status, decision, checkpoint_id "
            "FROM approvals ORDER BY approval_action",
        )
        assert len(approvals) == 2
        assert {row["approval_id"] for row in approvals} == {
            launch_approval_id,
            consumer_approval_id,
        }
        assert {row["approval_action"] for row in approvals} == {
            "launch_approval",
            "consumer_publish_approval",
        }
        assert {(row["status"], row["decision"]) for row in approvals} == {("approved", "approve")}
        checkpoint_ids = {
            row["checkpoint_id"] for row in _rows(platform, "SELECT checkpoint_id FROM checkpoints")
        }
        assert len(checkpoint_ids) > 20
        assert {row["checkpoint_id"] for row in approvals} <= checkpoint_ids
        assert saga[0]["checkpoint"] in checkpoint_ids

        inbox = _rows(
            platform,
            "SELECT adapter_id, source_event_id, event_type, processing_status, "
            "redacted_payload_json FROM integration_event_inbox",
        )
        assert len(inbox) == 4
        assert len({(row["adapter_id"], row["source_event_id"]) for row in inbox}) == 4
        assert {row["processing_status"] for row in inbox} == {"matched", "consumed"}
        merchant_payload = next(
            json.loads(row["redacted_payload_json"])
            for row in inbox
            if row["event_type"] == "merchant.enrollment_upserted"
        )
        assert merchant_payload["merchant_id"] == "[REDACTED]"
        assert tuple(
            _rows(
                platform,
                "SELECT COUNT(*) AS total, COUNT(DISTINCT event_id) AS unique_ids FROM outbox",
            )[0]
        ) == (4, 4)


@pytest.mark.asyncio
async def test_rejected_launch_is_not_presented_as_completed(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "rejected")
    result = await start_local_workflow(
        config,
        thread_id="scenario-a-rejected-view",
        campaign_id="campaign-rejected-view",
        user_request="拒绝路径展示",
    )
    approval_id = str(_interrupt(result, "launch_approval")["approval_id"])

    result = await decide_local_approval(
        config,
        thread_id="scenario-a-rejected-view",
        approval_id=approval_id,
        decision="reject",
        reason="fixture rejection",
    )

    # The legacy JSON status remains unchanged; the human projection owns the
    # corrected terminal meaning without breaking the automation contract.
    assert result.status == "completed"
    assert result.interrupts == ()
    assert result.view.terminal_outcome == "rejected"
    assert result.view.current_stage == "招商发布审批"
