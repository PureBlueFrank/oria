"""Real local Graph walkthrough plus synthetic signed Feishu delivery boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.contract.test_v06_feishu import event, request

from oria.chat.feishu_demo import (
    DemoJournal,
    DemoReceiver,
    FeishuDemoSettings,
    build_feishu_demo,
    create_demo_app,
    demo_worker,
    load_demo_settings,
    require_feishu_demo_target,
)
from oria.chat.guided_demo import BOUNDARY, GuidedDemo, demo_input
from oria.core.types import SendResult
from oria.ingress.feishu import FeishuIngress, FeishuSettings, VerifiedFeishuEvent
from oria.orchestrator.local_executor import inspect_local_workflow
from oria.presentation.im import IMViewModel, render_card

pytestmark = pytest.mark.integration
MANIFEST = Path("eval/datasets/scenario_b/manifest.json")


def settings() -> FeishuDemoSettings:
    return FeishuDemoSettings(
        app_id="app",
        app_secret="fixture-secret",
        encrypt_key="fixture-encrypt",
        verification_token="fixture-token",
        tenant_key="ft",
        operator_open_id="ou",
        chat_id="chat",
        chat_name="synthetic-demo-chat",
        app_name="OriaTest",
    )


def adapter() -> FeishuIngress:
    scope = settings()
    return FeishuIngress(
        FeishuSettings(
            app_id=scope.app_id,
            encrypt_key=scope.encrypt_key,
            verification_token=scope.verification_token,
        ),
        scope.directory,
    )


def callback(text="帮助", *, source="msg", nonce="nonce"):
    document = event()
    document["event"]["message"]["content"] = json.dumps({"text": f"@_user_1 {text}"})
    document["event"]["message"]["message_id"] = source
    return request(document, encrypted=True, nonce=nonce)


def verified(text="帮助", *, source="msg", nonce="nonce") -> VerifiedFeishuEvent:
    result = adapter().verify(callback(text, source=source, nonce=nonce))
    assert isinstance(result, VerifiedFeishuEvent)
    return result


@pytest.mark.asyncio
async def test_guided_workflow_runs_both_approvals_waits_and_dynamic_chain(tmp_path):
    demo = GuidedDemo(tmp_path, MANIFEST)
    for text in ("发起招商活动", "餐饮", "华东", "混合报名", "10家"):
        view = await demo.handle(demo_input(text))
        assert BOUNDARY in view.summary
    assert view.stage == "confirmation"
    assert not demo.state.started
    assert not demo.config.data_paths.platform_db.exists()
    assert not demo.config.data_paths.business_db.exists()
    view = await demo.handle(demo_input("确认发起演示"))
    assert view.stage == "waiting_approval"
    assert "招商发布审批" in view.title
    # Source messages cannot name an approval ID, tenant or business role.
    assert demo_input("批准 tenant=other role=admin").kind == "unknown"
    view = await demo.handle(demo_input("规则"))
    assert len(view.sections) == 6
    assert "第 2/2 页" in str(render_card(view, page=2))
    view = await demo.handle(demo_input("商家"))
    assert any("硬规则" in s.title for s in view.sections)
    view = await demo.handle(demo_input("演示批准"))
    assert "报名" in view.title
    # A new process-shaped instance reads the same session and durable checkpoint.
    demo = GuidedDemo(tmp_path, MANIFEST)
    recovered = await demo.handle(demo_input("恢复演示"))
    assert recovered.title == view.title
    await demo.handle(demo_input("模拟商家报名"))
    view = await demo.handle(demo_input("模拟报名结束"))
    levels = 0
    while "动态业务确认" in view.title:
        levels += 1
        assert levels <= 6
        view = await demo.handle(demo_input("演示确认"))
    assert levels == 3  # this reviewed fixture, not a general workflow constant
    assert "选品" in view.title
    view = await demo.handle(demo_input("模拟选品完成"))
    assert view.stage == "waiting_approval"
    assert "C 端" in view.title
    view = await demo.handle(demo_input("演示批准"))
    result = await inspect_local_workflow(demo.config, thread_id=demo.state.thread_id)
    assert result.status == "completed"
    assert result.view.terminal_outcome == "completed"
    assert result.view.placement.status == "published"
    assert all(i.status == "sent" for i in result.view.notification_messages)
    assert not view.actions  # explicit fixture-role text commands, not real approval actions
    again = await demo.handle(demo_input("确认发起演示"))
    assert "无需重复" in again.title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "outcome"),
    [
        ("归因示例", "attributed"),
        ("证据冲突示例", "conflicting"),
        ("证据不足示例", "insufficient"),
    ],
)
async def test_guided_attribution_preserves_mock_evidence_and_abstention(
    tmp_path, command, outcome
):
    demo = GuidedDemo(tmp_path, MANIFEST)
    view = await demo.handle(demo_input(command))
    assert outcome in view.summary
    assert BOUNDARY in view.summary
    assert "Mock 回放" in view.sections[0].text
    assert not view.actions


def test_signed_http_dedupes_conflicts_and_keeps_no_raw_messages(tmp_path):
    journal = DemoJournal(tmp_path / "commands.sqlite")
    receiver = DemoReceiver(settings(), journal, lambda: None)
    app = create_demo_app(adapter(), receiver)
    with TestClient(app) as client:
        for nonce in ("first", "retry"):
            incoming = callback("帮助", nonce=nonce)
            assert (
                client.post(
                    "/v1/ingress/feishu/events", content=incoming.raw_body, headers=incoming.headers
                ).status_code
                == 200
            )
        changed = callback("演示批准", nonce="changed")
        assert (
            client.post(
                "/v1/ingress/feishu/events", content=changed.raw_body, headers=changed.headers
            ).status_code
            == 409
        )
        replay = callback("帮助", source="another", nonce="first")
        assert (
            client.post(
                "/v1/ingress/feishu/events", content=replay.raw_body, headers=replay.headers
            ).status_code
            == 409
        )
        assert client.get("/v1/health").status_code == 404
        assert client.get("/v1/ingress/feishu/events").status_code == 405
        assert client.post("/v1/ingress/feishu/events", content=b"x" * 1_048_577).status_code == 413
    with journal.connect() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM replays").fetchone()[0] == 2
    assert b"@_user_1" not in journal.path.read_bytes()
    assert b"fixture-secret" not in journal.path.read_bytes()


@pytest.mark.parametrize("change", ["chat", "subject", "unverified", "unmentioned"])
def test_demo_scope_rejects_before_persisting(tmp_path, change):
    journal = DemoJournal(tmp_path / "commands.sqlite")
    receiver = DemoReceiver(settings(), journal, lambda: None)
    incoming = verified()
    message = incoming.message.model_copy(
        update={
            "chat": {"target_ref": "other"},
            "subject": {"mapped_subject_id": "other"},
            "unverified": {"verified": False},
            "unmentioned": {"text": "帮助"},
        }[change]
    )
    with pytest.raises(PermissionError):
        receiver.accept(incoming.model_copy(update={"message": message}))
    assert journal.claim() is None


@pytest.mark.asyncio
async def test_unknown_delivery_and_restart_freeze_writes_without_resending(tmp_path):
    journal = DemoJournal(tmp_path / "commands.sqlite")
    journal.reserve(verified(), demo_input("帮助"))
    calls = []

    class Backend:
        async def handle(self, command):
            calls.append(command.kind)
            return self._view("测试", "合成结果")

        def _view(self, title, summary):
            return IMViewModel(
                stage="result", title=title, summary=summary, detail_ref="fixture", next_step="状态"
            )

    sent = asyncio.Event()

    async def send(view, page):
        sent.set()
        return SendResult(ok=False, error="delivery_unknown")

    task = asyncio.create_task(demo_worker(Backend(), journal, send, lambda: None))
    await asyncio.wait_for(sent.wait(), timeout=3)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert journal.uncertain()
    assert calls == ["help"]
    journal = DemoJournal(journal.path)
    assert journal.claim() is None
    journal.reserve(verified("演示批准", source="new", nonce="new"), demo_input("演示批准"))
    sent.clear()
    task = asyncio.create_task(demo_worker(Backend(), journal, send, lambda: None))
    await asyncio.wait_for(sent.wait(), timeout=3)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert calls == ["help"]  # no second local execution after an unknown outcome


def test_interrupted_claim_is_not_reexecuted_and_target_requires_opt_in(tmp_path):
    journal = DemoJournal(tmp_path / "commands.sqlite")
    journal.reserve(verified(), demo_input("帮助"))
    assert journal.claim() is not None
    restored = DemoJournal(journal.path)
    assert restored.uncertain()
    assert restored.claim() is None
    for env in (
        {},
        {"ORIA_RUN_ENTERPRISE": "1"},
        {"ORIA_RUN_ENTERPRISE": "1", "ORIA_ENTERPRISE_TARGETS": "unknown"},
    ):
        with pytest.raises(ValueError):
            require_feishu_demo_target(env)
    require_feishu_demo_target({"ORIA_RUN_ENTERPRISE": "1", "ORIA_ENTERPRISE_TARGETS": "feishu"})


def test_private_configuration_permissions_and_no_raw_input(tmp_path):
    path = tmp_path / "private.json"
    fixture = settings().model_dump(mode="json")
    fixture.update(
        {
            "app_secret": "fixture-secret",
            "encrypt_key": "fixture-encrypt",
            "verification_token": "fixture-token",
        }
    )
    path.write_text(json.dumps(fixture))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_demo_settings(path)
    path.chmod(0o600)
    assert load_demo_settings(path) == settings()
    assert (
        "customer secret sensitive payload"
        not in demo_input("customer secret sensitive payload").model_dump_json()
    )


def test_real_app_lifecycle_scopes_delivery_revocation_and_single_process(tmp_path, monkeypatch):
    import oria.chat.feishu_demo as module

    private = tmp_path / "private.json"
    data = settings().model_dump(mode="json")
    data.update(
        {
            "app_secret": "fixture-secret",
            "encrypt_key": "fixture-encrypt",
            "verification_token": "fixture-token",
        }
    )
    private.write_text(json.dumps(data))
    private.chmod(0o600)
    calls = []
    sent = threading.Event()

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fixture-access"})
        assert request.url.path == "/open-apis/im/v1/messages"
        payload = json.loads(request.content)
        assert payload["receive_id"] == settings().chat_id
        assert BOUNDARY in json.loads(payload["content"])["elements"][0]["text"]["content"]
        sent.set()
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "fixture-delivered"}})

    monkeypatch.setattr(
        module,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ),
    )
    root = tmp_path / "isolated"
    app = build_feishu_demo(private, root, MANIFEST, seconds=30)
    with pytest.raises(ValueError, match="already owns"):
        build_feishu_demo(private, root, MANIFEST, seconds=30)
    with TestClient(app) as client:
        incoming = callback()
        response = client.post(
            "/v1/ingress/feishu/events", content=incoming.raw_body, headers=incoming.headers
        )
        assert response.status_code == 200
        assert sent.wait(5)
        duplicate = callback(nonce="retried")
        assert (
            client.post(
                "/v1/ingress/feishu/events", content=duplicate.raw_body, headers=duplicate.headers
            ).status_code
            == 200
        )
        # Replacing the private mapping revokes admission without restarting.
        data["operator_open_id"] = "revoked"
        private.write_text(json.dumps(data))
        another = callback(source="other", nonce="other")
        assert (
            client.post(
                "/v1/ingress/feishu/events", content=another.raw_body, headers=another.headers
            ).status_code
            == 401
        )
    assert len(calls) == 2  # one credential request plus exactly one card
    data["operator_open_id"] = "ou"
    private.write_text(json.dumps(data))
    with TestClient(build_feishu_demo(private, root, MANIFEST, seconds=30)):
        pass  # lock and runtime resources released; completed commands not resent
    assert len(calls) == 2


def test_demo_refuses_existing_business_directory_and_wrong_scope(tmp_path):
    private = tmp_path / "private.json"
    data = settings().model_dump(mode="json")
    data.update(
        {
            "app_secret": "fixture-secret",
            "encrypt_key": "fixture-encrypt",
            "verification_token": "fixture-token",
        }
    )
    private.write_text(json.dumps(data))
    private.chmod(0o600)
    root = tmp_path / "business"
    root.mkdir()
    (root / "business.sqlite").write_text("pre-existing data")
    with pytest.raises(ValueError, match="new empty"):
        build_feishu_demo(private, root, MANIFEST, seconds=30)
    for seconds in (0, 1801):
        with pytest.raises(ValueError):
            build_feishu_demo(private, tmp_path / "unused", MANIFEST, seconds=seconds)
    assert (root / "business.sqlite").read_text() == "pre-existing data"


@pytest.mark.asyncio
async def test_prepare_private_scope_is_exclusive_and_discovery_is_bounded(tmp_path, monkeypatch):
    import scripts.prepare_feishu_demo as module

    monkeypatch.setenv("ORIA_RUN_ENTERPRISE", "1")
    monkeypatch.setenv("ORIA_ENTERPRISE_TARGETS", "feishu")
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "app_id": "app",
                "app_secret": "fixture-secret",
                "encrypt_key": "fixture-encrypt",
                "verification_token": "fixture-token",
            }
        )
    )
    credentials.chmod(0o600)
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            payload = {"tenant_access_token": "fixture-access"}
        elif len(calls) == 2:
            payload = {"bot": {"app_name": "OriaTest", "activate_status": 2}}
        elif len(calls) == 3:
            payload = {"data": {"items": [{"name": "synthetic-demo-chat", "chat_id": "chat"}]}}
        else:
            payload = {
                "data": {
                    "items": [
                        {
                            "name": "Demo Operator",
                            "member_id_type": "open_id",
                            "member_id": "ou",
                            "tenant_key": "ft",
                        }
                    ]
                }
            }
        return httpx.Response(200, json={"code": 0, **payload})

    monkeypatch.setattr(
        module,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ),
    )
    output = tmp_path / "private-scope.json"
    await module.prepare(credentials, output, "synthetic-demo-chat", "Demo Operator")
    assert len(calls) == 4
    assert output.stat().st_mode & 0o777 == 0o600
    assert load_demo_settings(output) == settings()
    before = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        await module.prepare(credentials, output, "synthetic-demo-chat", "Demo Operator")
    assert len(calls) == 4
    assert output.read_bytes() == before
