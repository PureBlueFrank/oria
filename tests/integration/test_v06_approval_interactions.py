"""Reasoned rejection and authorized card details share the durable approval boundary."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tests.contract.test_v06_feishu import SETTINGS, request
from tests.integration.test_v06_approval_delivery import delivery_stack as delivery_stack
from tests.integration.test_v06_transactional_approval_acceptance import (
    _authenticator,
    _counts,
    _document,
)

from oria.api.app import create_app
from oria.api.identity import IdentityDirectory
from oria.api.server import _ApprovalOnlyReceiver
from oria.core.approvals import ApprovalBusinessBinding, ApprovalConflict
from oria.ingress.approval_cards import ApprovalCardBindings
from oria.ingress.feishu import CardAction, FeishuIngress
from oria.presentation.im import render_card

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _callback(action, *, reason=None, nonce="interaction", **kwargs):
    document = _document(action, **kwargs)
    if reason is not None:
        document["event"]["action"]["form_value"] = {"rejection_reason": reason}
    return request(document, encrypted=True, nonce=nonce)


@pytest.mark.asyncio
@pytest.mark.recovery
async def test_reasoned_rejection_restarts_and_retries_without_duplicate_decision(
    delivery_stack, monkeypatch
):
    import tests.contract.test_v06_feishu as feishu_fixture

    sent_at = datetime.now(UTC)

    class SenderClock:
        calls = 0

        @classmethod
        def now(cls, timezone):
            # The signing helper reads once for the header and once for received_at.
            instant = sent_at + timedelta(seconds=cls.calls // 2)
            cls.calls += 1
            return instant

    monkeypatch.setattr(feishu_fixture, "datetime", SenderClock)
    state = delivery_stack
    action = await state.bindings.issue(
        "approval-card",
        state.approver,
        chat_id="chat",
        message_id="card-msg",
        decision_intent="reject",
    )
    # Reconstruct the presentation service; no in-memory issuance state is required.
    cards = ApprovalCardBindings(
        state.runtime, state.repository, state.source, state.databases.platform_sessions
    )
    receiver = _ApprovalOnlyReceiver(state.receiver._receiver, cards)
    auth = _authenticator()
    app = create_app(
        authenticator=auth, feishu=FeishuIngress(SETTINGS, state.source), receiver=receiver
    )
    baseline = await _counts(state.databases)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local"
    ) as client:

        async def post(inbound):
            return await client.post(
                "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
            )

        inbound = _callback(action, reason="方案预算需要调整", nonce="first")
        response = await post(inbound)
        assert response.status_code == 200
        assert "已拒绝" in response.text and "方案预算需要调整" in response.text
        assert action.action_ref not in response.text and "checkpoint-secret" not in response.text
        committed = await _counts(state.databases)
        assert committed["ingress_receipts"] == committed["ingress_replays"] == 1
        assert committed["outbox"] == baseline["outbox"] + 1
        duplicate = await post(inbound)
        assert duplicate.json() == response.json()
        assert await _counts(state.databases) == committed
        # A re-signed retry has a new transport replay key, but the same action
        # receipt must still prevent another approval decision/audit/outbox.
        resigned = _callback(action, reason="方案预算需要调整", nonce="first")
        assert (
            dict(resigned.headers)["X-Lark-Request-Timestamp"]
            != dict(inbound.headers)["X-Lark-Request-Timestamp"]
        )
        retried = await post(resigned)
        assert retried.json() == response.json()
        committed = {**committed, "ingress_replays": committed["ingress_replays"] + 1}
        assert await _counts(state.databases) == committed
        changed = await post(_callback(action, reason="不同的理由", nonce="changed"))
        assert changed.status_code == 409
        assert await _counts(state.databases) == committed
        approval = await state.repository.get("tenant", "approval-card")
        assert approval.status == "rejected" and approval.reason == "方案预算需要调整"
    await auth.client.aclose()


@pytest.mark.parametrize("reason", [None, "", "   ", "x" * 1001, {"nested": "value"}])
@pytest.mark.asyncio
async def test_invalid_rejection_reason_never_claims_receipt(delivery_stack, reason):
    state = delivery_stack
    action = await state.bindings.issue(
        "approval-card",
        state.approver,
        chat_id="chat",
        message_id="card-msg",
        decision_intent="reject",
    )
    baseline = await _counts(state.databases)
    inbound = _callback(action, reason=reason)
    try:
        event = FeishuIngress(SETTINGS, state.source).verify(inbound)
        with pytest.raises((ValueError, PermissionError)):
            await state.receiver.accept(event)
    except PermissionError:
        pass
    assert await _counts(state.databases) == baseline
    assert (await state.repository.get("tenant", "approval-card")).status == "pending"


@pytest.mark.asyncio
async def test_detail_page_reauthorizes_without_deciding_or_creating_outbox(delivery_stack):
    state = delivery_stack
    action = await state.bindings.issue(
        "approval-card",
        state.approver,
        chat_id="chat",
        message_id="card-msg",
        decision_intent="view",
    )
    baseline = await _counts(state.databases)
    event = FeishuIngress(SETTINGS, state.source).verify(_callback(action))
    view = await state.receiver.accept(event)
    assert view.detail_ref == "approval-card"
    assert view.stage == "waiting_approval" and not view.actions and view.rejection_action is None
    assert await _counts(state.databases) == {
        **baseline,
        "ingress_receipts": 1,
        "ingress_replays": 1,
    }
    assert (await state.repository.get("tenant", "approval-card")).status == "pending"
    state.directory = IdentityDirectory()
    with pytest.raises(PermissionError):
        await state.receiver.accept(event)


@pytest.mark.asyncio
async def test_issued_card_contains_separate_opaque_approve_reject_and_detail_actions(
    delivery_stack,
):
    state = delivery_stack
    view = await state.bindings.issue_view(
        "approval-card", state.approver, chat_id="chat", message_id="card-msg"
    )
    card = render_card(view)
    assert card["schema"] == "2.0"
    form = next(element for element in card["body"]["elements"] if element["tag"] == "form")
    field, button = form["elements"]
    assert field["name"] == "rejection_reason" and field["required"]
    assert field["max_length"] == 1000
    assert button["form_action_type"] == "submit"
    assert (await _counts(state.databases))["approval_card_bindings"] == 3
    assert "checkpoint-secret" not in json.dumps(card)


@pytest.mark.asyncio
async def test_pages_keep_original_controls_and_retries_do_not_mint_bindings(delivery_stack):
    state = delivery_stack
    original = state.approval
    approval = original.model_copy(
        update={
            "approval_id": "approval-pages",
            "business_binding": ApprovalBusinessBinding(
                campaign_id="campaign",
                enrollment_version=1,
                link_version=1,
                selection_version="v1",
                rule_snapshot_hash="sha256:" + "b" * 64,
            ),
        }
    )
    await state.repository.add(approval)
    initial = await state.bindings.issue_view(
        "approval-pages", state.approver, chat_id="chat", message_id="card-msg"
    )
    action = initial.navigation[0].value.model_copy(update={"page": 2})
    event = FeishuIngress(SETTINGS, state.source).verify(_callback(action))
    baseline = await _counts(state.databases)
    view = await state.receiver.accept(event)
    assert view.page == 2 and view.actions == initial.actions
    assert view.rejection_action == initial.rejection_action
    card = json.dumps(render_card(view), ensure_ascii=False)
    assert "第 2/2 页" in card and "操作范围" not in card
    assert view.navigation[0].value.page == 1
    committed = await _counts(state.databases)
    assert committed["approval_card_bindings"] == baseline["approval_card_bindings"] == 3
    assert committed["outbox"] == baseline["outbox"]
    assert await state.receiver.accept(event) == view
    assert await _counts(state.databases) == committed
    invalid = FeishuIngress(SETTINGS, state.source).verify(
        _callback(action.model_copy(update={"page": 3}), nonce="invalid-page")
    )
    with pytest.raises(PermissionError):
        await state.receiver.accept(invalid)
    assert await _counts(state.databases) == committed
    # Choosing a display page never authorizes a decision action.
    tampered = FeishuIngress(SETTINGS, state.source).verify(
        _callback(initial.actions[0].value.model_copy(update={"page": 2}), nonce="approve-page")
    )
    with pytest.raises(PermissionError):
        await state.receiver.accept(tampered)
    assert (await state.repository.get("tenant", "approval-pages")).status == "pending"


@pytest.mark.asyncio
async def test_approve_and_reasoned_reject_compete_for_one_complete_snapshot(delivery_stack):
    state = delivery_stack
    view = await state.bindings.issue_view(
        "approval-card", state.approver, chat_id="chat", message_id="card-msg"
    )
    baseline = await _counts(state.databases)
    events = [
        FeishuIngress(SETTINGS, state.source).verify(
            _callback(view.actions[0].value, nonce="approve")
        ),
        FeishuIngress(SETTINGS, state.source).verify(
            _callback(view.rejection_action, reason="方案需要调整", nonce="reject")
        ),
    ]
    outcomes = await asyncio.gather(
        *(state.receiver.accept(event) for event in events), return_exceptions=True
    )
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ApprovalConflict) for outcome in outcomes) == 1
    counts = await _counts(state.databases)
    assert counts["ingress_receipts"] == counts["ingress_replays"] == 1
    assert counts["outbox"] == baseline["outbox"] + 1
    assert counts["audit_events"] == baseline["audit_events"] + 1


@pytest.mark.asyncio
async def test_form_reason_cannot_change_a_server_bound_approve_intent(delivery_stack):
    state = delivery_stack
    action = await state.bindings.issue(
        "approval-card", state.approver, chat_id="chat", message_id="card-msg"
    )
    baseline = await _counts(state.databases)
    event = FeishuIngress(SETTINGS, state.source).verify(_callback(action, reason="不要批准"))
    with pytest.raises(PermissionError):
        await state.receiver.accept(event)
    assert await _counts(state.databases) == baseline


@pytest.mark.asyncio
async def test_detail_of_changed_revision_hides_stale_decisions_and_explains_recovery(
    delivery_stack,
):
    state = delivery_stack
    initial = await state.bindings.issue_view(
        "approval-card", state.approver, chat_id="chat", message_id="card-msg"
    )
    await state.repository.replace(
        state.approval.model_copy(update={"updated_at": datetime.now(UTC)}),
        expected=state.approval,
    )
    event = FeishuIngress(SETTINGS, state.source).verify(_callback(initial.navigation[0].value))
    current = await state.receiver.accept(event)
    assert not current.actions and current.rejection_action is None
    assert "重新发送" in current.next_step


@pytest.mark.parametrize("page", [0, 21, True, "2"])
def test_page_selector_requires_a_bounded_integer(page):
    with pytest.raises(ValueError):
        CardAction(action_ref="opaque-reference", page=page)
