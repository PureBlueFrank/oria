"""Approval delivery/activation and signed callbacks over real Runtime/SQLite."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from tests.contract.test_v06_feishu import SETTINGS, request
from tests.integration.test_v06_transactional_approval_acceptance import (
    _approval,
    _authenticator,
    _counts,
    _document,
)
from tests.security.test_v06_api_identity import binding

from oria.api.app import create_app
from oria.api.identity import IdentityDirectory
from oria.api.server import _ApprovalOnlyReceiver
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.ingress.approval_cards import ApprovalCardBindings, TransactionalApprovalCardAcceptor
from oria.ingress.approval_delivery import ApprovalCardDelivery
from oria.ingress.feishu import CardAction, FeishuIngress
from oria.ingress.feishu_channel import FeishuChannel
from oria.ingress.receipts import IngressUnavailable, TransactionalFeishuReceiver
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest_asyncio.fixture
async def delivery_stack(tmp_path):
    approver = binding(issuer="feishu:app:ft", subject="ou")
    sender = binding(issuer="https://identity.example", subject="sender")
    sender = sender.model_copy(
        update={
            "actor": sender.actor.model_copy(
                update={"subject_id": "requester", "roles": ("campaign_admin",)}
            )
        }
    )
    state = SimpleNamespace(
        directory=IdentityDirectory(bindings=(sender, approver)),
        sender=sender,
        approver=approver,
        calls=[],
        mode="success",
        tokens=0,
    )
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    async with DatabaseResources(config) as databases:
        runtime = await build_runtime(
            config,
            resource_factories=(lambda: databases,),
            trusted_actors=(sender.actor, approver.actor),
            trusted_executors=(sender.executor, approver.executor),
        )
        async with runtime:
            repository = SQLiteApprovalRepository(databases.platform_sessions)
            state.approval = await _approval(repository, datetime.now(UTC))

            def source():
                return state.directory

            bindings = ApprovalCardBindings(
                runtime, repository, source, databases.platform_sessions
            )

            async def transport(req):
                state.calls.append((req.method, req.url, json.loads(req.content)))
                if state.mode == req.method + "_timeout":
                    raise httpx.ReadTimeout("sensitive provider body")
                if state.mode == req.method + "_rejected":
                    return httpx.Response(200, json={"code": 230001})
                if req.method == "POST":
                    if state.mode == "revoked_after_send":
                        state.directory = IdentityDirectory(bindings=(sender,))
                    return httpx.Response(200, json={"code": 0, "data": {"message_id": "card-msg"}})
                # The binding must already be committed when buttons become visible.
                assert (await _counts(databases))["approval_card_bindings"] == 1
                return httpx.Response(200, json={"code": 0})

            async def token(tenant):
                assert tenant == "tenant"
                state.tokens += 1
                if state.mode == "revoked_before_send" or (
                    state.mode == "revoked_before_patch" and state.tokens == 2
                ):
                    state.directory = IdentityDirectory(bindings=(sender,))
                return SecretStr("fixture-access-token")

            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                channel = FeishuChannel(
                    client, token, {("tenant", "chat"): frozenset({"requester"})}
                )
                state.delivery = ApprovalCardDelivery(
                    runtime, bindings, source, channel, {("tenant", "chat"): frozenset({"actor"})}
                )
                state.receiver = _ApprovalOnlyReceiver(
                    TransactionalFeishuReceiver(
                        databases.platform_sessions,
                        source,
                        TransactionalApprovalCardAcceptor(runtime, repository, source),
                    ),
                    bindings,
                )
                state.source = source
                state.channel = channel
                state.bindings = bindings
                state.runtime = runtime
                state.repository = repository
                state.databases = databases
                yield state


async def _deliver(state, **kwargs):
    return await state.delivery.deliver(
        "approval-card",
        sender=state.sender,
        approver=state.approver,
        chat_id="chat",
        delivery_id="approval-delivery-1",
        **kwargs,
    )


def _action(state):
    active = json.loads(state.calls[-1][2]["content"])
    return CardAction.model_validate(active["elements"][-1]["actions"][0]["value"])


@pytest.mark.asyncio
@pytest.mark.recovery
async def test_delivery_binds_confirmed_message_before_activation_and_callback_updates(
    delivery_stack,
):
    state = delivery_stack
    result = await _deliver(state)
    assert result.ok and result.message_id == "card-msg"
    assert [method for method, _, _ in state.calls] == ["POST", "PATCH"]
    inactive = json.loads(state.calls[0][2]["content"])
    active = json.loads(state.calls[1][2]["content"])
    assert inactive["config"]["update_multi"] and active["config"]["update_multi"]
    assert all(item["tag"] != "action" for item in inactive["elements"])
    assert state.calls[1][1].path == "/open-apis/im/v1/messages/card-msg"
    assert set(state.calls[1][2]) == {"content"}
    assert "checkpoint-secret" not in json.dumps(active)
    action = _action(state)
    auth = _authenticator()
    app = create_app(
        authenticator=auth,
        feishu=FeishuIngress(SETTINGS, state.source),
        receiver=state.receiver,
    )
    inbound = request(_document(action))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local"
    ) as client:
        wrong = request(_document(action, message_id="foreign-message"))
        assert (
            await client.post(
                "/v1/ingress/feishu/events", content=wrong.raw_body, headers=dict(wrong.headers)
            )
        ).status_code == 404
        response = await client.post(
            "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
        )
        assert response.status_code == 200
        card = response.json()["card"]
        assert card["type"] == "raw" and card["data"]["config"]["update_multi"]
        assert "已批准" in response.text
        assert "checkpoint-secret" not in response.text and action.action_ref not in response.text
        assert all(item["tag"] != "action" for item in card["data"]["elements"])
        committed = await _counts(state.databases)
        assert committed["ingress_receipts"] == 1
        assert (await state.repository.get("tenant", "approval-card")).status == "approved"
        duplicate = await client.post(
            "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
        )
        assert duplicate.json() == response.json()
        assert await _counts(state.databases) == committed
    await auth.client.aclose()


@pytest.mark.parametrize(
    ("mode", "methods", "bindings", "error"),
    [
        ("POST_timeout", ["POST"], 0, "delivery_unknown"),
        ("POST_rejected", ["POST"], 0, "provider_rejected"),
        ("PATCH_timeout", ["POST", "PATCH"], 1, "delivery_unknown"),
        ("PATCH_rejected", ["POST", "PATCH"], 1, "provider_rejected"),
        ("revoked_after_send", ["POST"], 0, "card_binding_unavailable"),
        ("revoked_before_patch", ["POST"], 1, "card_binding_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_partial_delivery_never_resends_or_activates_without_binding(
    delivery_stack, mode, methods, bindings, error
):
    state = delivery_stack
    state.mode = mode
    result = await _deliver(state)
    assert not result.ok and result.error == error
    assert [method for method, _, _ in state.calls] == methods
    counts = await _counts(state.databases)
    assert counts["approval_card_bindings"] == bindings
    assert counts["ingress_receipts"] == counts["ingress_replays"] == 0
    assert (await state.repository.get("tenant", "approval-card")).status == "pending"


@pytest.mark.parametrize(
    "change", ["destination", "revoked", "self_approval", "cross_tenant", "revoked_before_send"]
)
@pytest.mark.asyncio
async def test_delivery_denies_invalid_recipient_before_any_network(delivery_stack, change):
    state = delivery_stack
    chat_id = "foreign" if change == "destination" else "chat"
    approval_id = "approval-card"
    if change == "revoked":
        state.directory = IdentityDirectory(bindings=(state.sender,))
    elif change == "revoked_before_send":
        state.mode = change
    elif change == "self_approval":
        approval_id = "own-approval"
        await state.repository.add(
            state.approval.model_copy(
                update={"approval_id": approval_id, "requester": state.approver.actor.subject_id}
            )
        )
    elif change == "cross_tenant":
        state.sender = state.sender.model_copy(
            update={
                "actor": state.sender.actor.model_copy(update={"tenant_id": "foreign"}),
                "executor": state.sender.executor.model_copy(update={"tenant_id": "foreign"}),
            }
        )
        state.directory = IdentityDirectory(bindings=(state.sender, state.approver))
    with pytest.raises(PermissionError):
        await state.delivery.deliver(
            approval_id,
            sender=state.sender,
            approver=state.approver,
            chat_id=chat_id,
            delivery_id="delivery-denied",
        )
    assert state.calls == [] and state.tokens == (1 if change == "revoked_before_send" else 0)
    assert (await _counts(state.databases))["approval_card_bindings"] == 0


@pytest.mark.asyncio
async def test_channel_update_requires_confirmed_shared_card_owner(delivery_stack):
    state = delivery_stack
    ctx = state.runtime.new_context(
        actor=state.sender.actor,
        executor=state.sender.executor,
        session_id="s",
        thread_id="t",
        run_id="r",
    )
    view = await state.bindings.pending_view("approval-card", state.approver)
    assert (await state.channel.update_view("chat", "unconfirmed", view, ctx)).error == (
        "recipient_denied"
    )
    assert state.calls == [] and state.tokens == 0
    # A normal non-shared card does not authorize PATCH, even with a valid receipt.
    assert (await state.channel.send_view("chat", view, ctx)).ok
    assert not (await state.channel.update_view("chat", "card-msg", view, ctx)).ok
    assert len(state.calls) == 1
    assert (await state.channel.send_view("chat", view, ctx, shared_update=True)).ok
    assert not (await state.channel.update_view("other-chat", "card-msg", view, ctx)).ok
    foreign_ctx = state.runtime.new_context(
        actor=state.approver.actor,
        executor=state.approver.executor,
        session_id="a",
        thread_id="a",
        run_id="a",
    )
    assert not (await state.channel.update_view("chat", "card-msg", view, foreign_ctx)).ok
    assert len(state.calls) == 2


@pytest.mark.asyncio
@pytest.mark.recovery
async def test_callback_projection_failure_retries_committed_outcome_without_new_decision(
    delivery_stack, monkeypatch
):
    state = delivery_stack
    assert (await _deliver(state)).ok
    auth = _authenticator()
    app = create_app(
        authenticator=auth,
        feishu=FeishuIngress(SETTINGS, state.source),
        receiver=state.receiver,
    )
    projection = state.bindings.accepted_view

    async def unavailable(_):
        raise IngressUnavailable("approval card storage unavailable")

    monkeypatch.setattr(state.bindings, "accepted_view", unavailable)
    inbound = request(_document(_action(state)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local"
    ) as client:
        first = await client.post(
            "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
        )
        assert first.status_code == 503 and "card" not in first.json()
        assert (await state.repository.get("tenant", "approval-card")).status == "approved"
        committed = await _counts(state.databases)
        monkeypatch.setattr(state.bindings, "accepted_view", projection)
        retry = await client.post(
            "/v1/ingress/feishu/events", content=inbound.raw_body, headers=dict(inbound.headers)
        )
        assert retry.status_code == 200 and "已批准" in retry.text
        assert await _counts(state.databases) == committed
    await auth.client.aclose()
