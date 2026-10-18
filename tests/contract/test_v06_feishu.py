"""Feishu wire contract and rejection paths using synthetic signed callbacks."""

# ruff: noqa: RUF001

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from pydantic import SecretStr
from tests.security.test_v06_api_identity import binding

from oria.api.app import create_app
from oria.api.identity import IdentityDirectory, JWTAuthenticator, JWTSettings
from oria.core.types import InboundRequest
from oria.ingress.feishu import CardAction, FeishuIngress, FeishuSettings, VerifiedFeishuEvent
from oria.ingress.feishu_channel import FeishuChannel
from oria.ingress.local import IngressVerificationError
from oria.permission.local import LocalPolicyEngine
from oria.presentation.im import IMAction, IMSection, IMViewModel, render_card

pytestmark = pytest.mark.contract

SETTINGS = FeishuSettings(
    app_id="app", encrypt_key="fixture-encrypt", verification_token="fixture-token"
)


def event():
    return {
        "schema": "2.0",
        "header": {
            "app_id": "app",
            "tenant_key": "ft",
            "token": "fixture-token",
            "event_id": "evt",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "tenant_key": "ft", "sender_id": {"open_id": "ou"}},
            "message": {
                "message_type": "text",
                "message_id": "msg",
                "chat_id": "chat",
                "content": json.dumps(
                    {"text": "查询活动", "tenant_id": "victim", "roles": ["admin"]}
                ),
            },
        },
    }


def request(document, *, encrypted=False, timestamp=None, nonce="nonce"):
    raw = json.dumps(document).encode()
    if encrypted:
        key = hashlib.sha256(b"fixture-encrypt").digest()
        iv = bytes(range(16))
        pad = padding.PKCS7(128).padder()
        padded = pad.update(raw) + pad.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        data = iv + encryptor.update(padded) + encryptor.finalize()
        raw = json.dumps({"encrypt": base64.b64encode(data).decode()}).encode()
    stamp = str(timestamp if timestamp is not None else int(datetime.now(UTC).timestamp()))
    signature = hashlib.sha256((stamp + nonce + "fixture-encrypt").encode() + raw).hexdigest()
    return InboundRequest(
        raw_body=raw,
        received_at=datetime.now(UTC),
        request_id="request",
        headers={
            "X-Lark-Request-Timestamp": stamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
    )


def ingress():
    directory = IdentityDirectory(bindings=(binding("feishu:app:ft", "ou"),))
    return FeishuIngress(SETTINGS, lambda: directory)


@pytest.mark.parametrize("encrypted", [False, True])
def test_text_verification_uses_server_identity_and_stable_source_key(encrypted):
    adapter = ingress()
    first = adapter.verify(request(event(), encrypted=encrypted))
    retry_doc = event()
    retry_doc["header"]["event_id"] = "different-delivery"
    second = adapter.verify(request(retry_doc, encrypted=encrypted, nonce="retry"))
    assert isinstance(first, VerifiedFeishuEvent) and isinstance(second, VerifiedFeishuEvent)
    assert first.message.mapped_tenant_id == "tenant"
    assert first.message.text == "查询活动"
    assert first.message.dedupe_key == second.message.dedupe_key
    assert first.replay_key != second.replay_key
    assert "raw_body" not in first.model_dump_json()


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    ["signature", "old", "future", "token", "app", "tenant", "user", "unsigned", "malformed"],
)
def test_invalid_callbacks_rejected(change):
    doc = event()
    if change in {"token", "app", "tenant"}:
        key = {"token": "token", "app": "app_id", "tenant": "tenant_key"}[change]
        doc["header"][key] = "wrong"
    if change == "user":
        doc["event"]["sender"]["sender_id"]["open_id"] = "unmapped"
    kwargs = {}
    if change == "old":
        kwargs["timestamp"] = 1
    if change == "future":
        kwargs["timestamp"] = 9999999999
    req = request(doc, **kwargs)
    if change == "signature":
        req = req.model_copy(update={"raw_body": req.raw_body + b" "})
    if change == "unsigned":
        req = req.model_copy(update={"headers": {}})
    if change == "malformed":
        req = request(["invalid"])
    with pytest.raises(IngressVerificationError, match="invalid Feishu callback"):
        ingress().verify(req)


def test_challenge_and_action_reference_contract():
    adapter = ingress()
    doc = {"type": "url_verification", "token": "fixture-token", "challenge": "challenge-code"}
    req = request(doc, encrypted=True).model_copy(update={"headers": {}})
    assert adapter.verify(req) == "challenge-code"
    doc = event()
    doc["header"]["event_type"] = "card.action.trigger"
    doc["event"] = {
        "operator": {"tenant_key": "ft", "open_id": "ou"},
        "context": {"open_message_id": "card-msg", "open_chat_id": "chat"},
        "action": {"value": {"version": 1, "action_ref": "approval-action"}},
    }
    result = adapter.verify(request(doc))
    assert result.kind == "action" and result.action.action_ref == "approval-action"
    doc["event"]["action"]["value"]["tenant_id"] = "victim"
    with pytest.raises(IngressVerificationError):
        adapter.verify(request(doc))


def test_go_callback_timestamp_preserves_expiry_and_original_signature():
    now = datetime(2026, 10, 18, 1, 0, 0, 123456, tzinfo=UTC)
    timestamp = "2026-10-18 09:00:00.123456789 +0800 CST m=+123456.987654321"
    doc = event()
    doc["header"]["event_type"] = "card.action.trigger"
    doc["event"] = {
        "operator": {"tenant_key": "ft", "open_id": "ou"},
        "context": {"open_message_id": "card-msg", "open_chat_id": "chat"},
        "action": {"value": {"version": 1, "action_ref": "approval-action"}},
    }
    inbound = request(doc, timestamp=timestamp, encrypted=True).model_copy(
        update={"received_at": now}
    )
    result = ingress().verify(inbound)
    assert isinstance(result, VerifiedFeishuEvent) and result.kind == "action"
    assert result.expires_at == pytest.approx(now.timestamp() + 300, rel=0, abs=0.000001)
    headers = dict(inbound.headers)
    headers["X-Lark-Request-Timestamp"] = timestamp.replace("m=+123456", "m=+654321")
    with pytest.raises(IngressVerificationError):
        ingress().verify(inbound.model_copy(update={"headers": headers}))


@pytest.mark.security
@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-18 00:54:59 +0000 UTC m=+999999.1",
        "2026-10-18 01:05:01 +0000 UTC m=+0.1",
        "2026-02-30 01:00:00 +0000 UTC m=+1.0",
        "2026-10-18 01:00:00 +2500 UTC m=+1.0",
        "2026-10-18 01:00:00.1234567890 +0000 UTC m=+1.0",
        "2026-10-18 01:00:00 +0000 UTC m=nan",
        "2026-10-18T01:00:00Z",
    ],
)
def test_invalid_go_timestamp_never_bypasses_time_window(timestamp):
    inbound = request(event(), timestamp=timestamp).model_copy(
        update={"received_at": datetime(2026, 10, 18, 1, 0, 0, tzinfo=UTC)}
    )
    with pytest.raises(IngressVerificationError):
        ingress().verify(inbound)


def test_go_timestamp_without_monotonic_suffix_obeys_configured_shorter_window():
    now = datetime(2026, 10, 18, 1, 0, 0, tzinfo=UTC)
    adapter = FeishuIngress(SETTINGS.model_copy(update={"max_age_seconds": 5}), ingress().directory)
    fresh = request(event(), timestamp="2026-10-18 01:00:00 +0000 UTC").model_copy(
        update={"received_at": now}
    )
    assert adapter.verify(fresh).expires_at == now.timestamp() + 5
    stale = request(event(), timestamp="2026-10-18 00:59:54 +0000 UTC m=+999999.0").model_copy(
        update={"received_at": now}
    )
    with pytest.raises(IngressVerificationError):
        adapter.verify(stale)


def test_no_acknowledgement_without_durable_receiver():
    directory = IdentityDirectory()
    auth = JWTAuthenticator(
        JWTSettings(
            issuer="https://identity.example",
            audience="oria",
            jwks_url="https://identity.example/keys",
        ),
        lambda: directory,
        httpx.AsyncClient(),
    )
    app = create_app(authenticator=auth, feishu=ingress())
    req = request(event())
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(
                "/v1/ingress/feishu/events", content=req.raw_body, headers=dict(req.headers)
            )
            assert response.status_code == 503
            assert response.json()["retryable"]
            assert "查询活动" not in response.text
        oversized = client.post("/v1/ingress/feishu/events", content=b"x" * 1_048_577)
        assert oversized.status_code == 413


def views():
    for stage in (
        "received",
        "confirmation",
        "progress",
        "result",
        "waiting_approval",
        "waiting_event",
        "failed",
    ):
        yield IMViewModel(
            stage=stage,
            title="招商活动",
            summary="请查看当前进度",
            detail_ref="detail-123",
            next_step="需要帮助时请提供详情编号并联系管理员。",
            sections=(IMSection(title="规则", text="合成规则 v1"),),
            actions=(IMAction(label="确认", value=CardAction(action_ref="action-123")),)
            if stage in {"confirmation", "waiting_approval"}
            else (),
        )


def test_card_golden_all_stages_and_pagination_redaction():
    expected = json.loads((Path(__file__).parent / "fixtures/feishu-cards-v1.json").read_text())
    assert {view.stage: render_card(view) for view in views()} == expected
    view = next(views()).model_copy(
        update={
            "sections": tuple(IMSection(title=f"证据 {i}", text="合成摘要") for i in range(7)),
            "summary": "联系 demo@example.com 或 13800138000，token=secret-value",
        }
    )
    card = json.dumps(render_card(view, page=2), ensure_ascii=False)
    assert "证据 5" in card and "证据 0" not in card and "第 2/2 页" in card
    assert "secret-value" not in card and "demo@example.com" not in card
    with pytest.raises(ValueError):
        render_card(view, page=3)
    with pytest.raises(ValueError):
        IMViewModel.model_validate(next(views()).model_dump() | {"raw_tool_json": {}})


@pytest.mark.asyncio
async def test_channel_authorization_redaction_and_unknown_delivery():
    identity = binding()
    actor = identity.actor.model_copy(update={"roles": ("campaign_admin",)})
    ctx = SimpleNamespace(
        actor=actor,
        executor=identity.executor,
        tenant_id="tenant",
        correlation_id="request",
        policy=LocalPolicyEngine(trusted_actors=[actor], trusted_executors=[identity.executor]),
    )
    sent = []

    async def token(tenant):
        assert tenant == "tenant"
        return SecretStr("fixture-access-token")

    def transport(req):
        sent.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer fixture-access-token"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "outbound-1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = FeishuChannel(client, token, {("tenant", "chat"): frozenset({"actor"})})
        assert not (await channel.send_message("unmapped", "hello", ctx)).ok
        assert sent == []
        result = await channel.send_message("chat", "邮箱 demo@example.com", ctx)
        assert result.ok and result.message_id == "outbound-1"
        assert "demo@example.com" not in sent[0]["content"]
        assert (await channel.send_view("chat", next(views()), ctx, delivery_id="delivery-1")).ok
        assert sent[1]["uuid"] == "delivery-1"
        assert not (await channel.send_file("chat", "/not-read", ctx)).ok

    def timeout(req):
        raise httpx.ReadTimeout("sensitive provider body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        channel = FeishuChannel(client, token, {("tenant", "chat"): frozenset({"actor"})})
        result = await channel.send_message("chat", "hello", ctx)
        assert result.error == "delivery_unknown"
