"""Opt-in, time-bounded Feishu walkthrough; not the production API/Job receiver."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from oria.api.identity import IdentityBinding, IdentityDirectory
from oria.chat.guided_demo import DemoInput, GuidedDemo, demo_input
from oria.core.runtime import build_runtime
from oria.core.types import InboundRequest, SendResult, ValueModel
from oria.data import initialize_data
from oria.ingress.feishu import FeishuIngress, FeishuSettings, VerifiedFeishuEvent
from oria.ingress.feishu_channel import FeishuChannel
from oria.ingress.local import IngressVerificationError
from oria.orchestrator.local_executor import campaign_admin
from oria.permission.local import local_cli_executor
from oria.presentation.im import IMViewModel


class FeishuDemoSettings(ValueModel):
    """Private, server-owned scope, prepared after read-only discovery."""

    app_id: str = Field(min_length=1)
    app_secret: SecretStr
    encrypt_key: SecretStr
    verification_token: SecretStr
    tenant_key: str = Field(min_length=1)
    operator_open_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    chat_name: str = Field(min_length=1)
    app_name: str = Field(pattern=r"^OriaTest$")

    @property
    def scope(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    self.app_id,
                    self.tenant_key,
                    self.operator_open_id,
                    self.chat_id,
                ]
            ).encode()
        ).hexdigest()

    def directory(self) -> IdentityDirectory:
        # An isolated demo controller, never an enterprise identity directory.
        return IdentityDirectory(
            bindings=(
                IdentityBinding(
                    issuer=f"feishu:{self.app_id}:{self.tenant_key}",
                    subject=self.operator_open_id,
                    actor=campaign_admin(),
                    executor=local_cli_executor(),
                ),
            )
        )


def load_demo_settings(path: Path) -> FeishuDemoSettings:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("demo configuration must be a private regular file (0600)")
    return FeishuDemoSettings.model_validate_json(path.read_bytes())


def require_feishu_demo_target(environ: Mapping[str, str]) -> None:
    if (
        environ.get("ORIA_RUN_ENTERPRISE") != "1"
        or environ.get("ORIA_ENTERPRISE_TARGETS") != "feishu"
    ):
        raise ValueError("select ORIA_RUN_ENTERPRISE=1 and ORIA_ENTERPRISE_TARGETS=feishu")


class DemoJournal:
    """Small demo command journal, not a Job service or a delivery retry ledger.

    Canonical allowlisted inputs only; raw text, callback bodies and private
    identities never enter it. Unknown execution/delivery outcomes freeze writes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commands (
                    sequence INTEGER PRIMARY KEY, source_hash TEXT UNIQUE NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, response TEXT);
                CREATE TABLE IF NOT EXISTS replays (
                    replay_key TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL);
                UPDATE commands SET status='needs_review'
                    WHERE status IN ('executing','delivering');
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=2)

    def reserve(self, event: VerifiedFeishuEvent, command: DemoInput) -> bool:
        source = hashlib.sha256(event.message.dedupe_key.encode()).hexdigest()
        content = hashlib.sha256(event.message.text.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if event.expires_at <= datetime.now(UTC).timestamp():
                raise PermissionError("expired callback")
            replay = db.execute(
                "SELECT source_hash,content_hash FROM replays WHERE replay_key=?",
                (event.replay_key,),
            ).fetchone()
            if replay is not None and replay != (source, content):
                raise ValueError("replay conflict")
            if replay is None and db.execute("SELECT count(*) FROM replays").fetchone()[0] >= 160:
                raise PermissionError("demo replay budget exhausted")
            prior = db.execute(
                "SELECT content_hash FROM commands WHERE source_hash=?", (source,)
            ).fetchone()
            if prior is not None and prior[0] != content:
                raise ValueError("source conflict")
            if db.execute("SELECT count(*) FROM commands").fetchone()[0] >= 80 and prior is None:
                raise PermissionError("demo command budget exhausted")
            db.execute(
                "INSERT OR IGNORE INTO replays VALUES (?,?,?)", (event.replay_key, source, content)
            )
            if prior is not None:
                return False
            db.execute(
                "INSERT INTO commands(source_hash,content_hash,payload,status) "
                "VALUES (?,?,?,'queued')",
                (source, content, command.model_dump_json()),
            )
        return True

    def claim(self) -> tuple[int, DemoInput] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,payload FROM commands WHERE status='queued' "
                "ORDER BY sequence LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE commands SET status='executing' WHERE sequence=?", (row[0],))
            return row[0], DemoInput.model_validate_json(row[1])

    def uncertain(self) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM commands "
                    "WHERE status IN ('needs_review','delivery_unknown') LIMIT 1"
                ).fetchone()
            )

    def update(self, sequence: int, status: str, view: IMViewModel | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE commands SET status=?,response=COALESCE(?,response) WHERE sequence=?",
                (status, view.model_dump_json() if view else None, sequence),
            )

    def latest_view(self) -> IMViewModel | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT response FROM commands WHERE status='sent' AND response IS NOT NULL "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            return IMViewModel.model_validate_json(row[0]) if row else None


SendView = Callable[[IMViewModel, int], Awaitable[SendResult]]


class DemoReceiver:
    def __init__(
        self, settings: FeishuDemoSettings, journal: DemoJournal, check: Callable[[], None]
    ) -> None:
        self.settings, self.journal, self.check = settings, journal, check

    def accept(self, event: VerifiedFeishuEvent) -> None:
        self.check()
        expected = self.settings.directory().bindings[0]
        if (
            event.kind != "message"
            or event.binding != expected
            or event.message.target_ref != self.settings.chat_id
            or event.message.mapped_tenant_id != expected.actor.tenant_id
            or event.message.mapped_subject_id != expected.actor.subject_id
            or event.message.source != "feishu"
            or not event.message.verified
        ):
            raise PermissionError("outside demonstration scope")
        # Receive-message events use placeholder mentions. Require a leading @;
        # the app subscribes only to user @-bot messages, not whole-group history.
        match = re.fullmatch(r"@_user_[0-9]+\s+(.{1,500})", event.message.text.strip(), re.DOTALL)
        if match is None:
            raise PermissionError("a group @ command is required")
        self.journal.reserve(event, demo_input(match[1].strip()))


async def demo_worker(
    demo: GuidedDemo, journal: DemoJournal, send: SendView, check: Callable[[], None]
) -> None:
    while True:
        claimed = journal.claim()
        if claimed is None:
            await asyncio.sleep(0.2)
            continue
        sequence, command = claimed
        try:
            check()
            page = 1
            if command.kind == "page":
                prior = journal.latest_view()
                if prior is not None and command.page <= max(1, (len(prior.sections) + 4) // 5):
                    view, page = prior, command.page
                else:
                    view = demo._view("页码不可用", "请先发送规则或状态，再查询其有效页码。")
            elif journal.uncertain() and command.kind not in {
                "help",
                "status",
                "rules",
                "merchants",
            }:
                view = demo._view(
                    "演示已暂停",
                    "上一步执行或发送结果未确认。请查询状态，由维护者核对；不会自动重试。",
                )
            else:
                view = await demo.handle(command)
                if journal.uncertain():
                    view = view.model_copy(
                        update={
                            "summary": "演示已暂停，上次执行或发送结果待维护者核对。"
                            "以下仅为只读查询。\n\n" + view.summary
                        }
                    )
            check()
            journal.update(sequence, "delivering", view)
            sent = await send(view, page)
            journal.update(sequence, "sent" if sent.ok else "delivery_unknown")
        except asyncio.CancelledError:
            journal.update(sequence, "needs_review")
            raise
        except Exception:
            # Exceptions may embed private provider responses; never serialize them.
            journal.update(sequence, "needs_review")


def create_demo_app(
    ingress: FeishuIngress, receiver: DemoReceiver, lifespan: Any = None
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.post("/v1/ingress/feishu/events")
    async def callback(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_048_576:
                return JSONResponse({"code": "too_large"}, status_code=413)
        try:
            receiver.check()
            result = ingress.verify(
                InboundRequest(
                    raw_body=bytes(body),
                    headers=dict(request.headers),
                    received_at=datetime.now(UTC),
                    request_id="demo-callback",
                )
            )
            if isinstance(result, str):
                return JSONResponse({"challenge": result})
            receiver.accept(result)
            return JSONResponse({"code": 0})
        except (IngressVerificationError, PermissionError):
            return JSONResponse({"code": "denied"}, status_code=401)
        except ValueError:
            return JSONResponse({"code": "conflict"}, status_code=409)
        except Exception:
            return JSONResponse({"code": "unavailable"}, status_code=503)

    return app


def build_feishu_demo(config: Path, data_dir: Path, manifest: Path, *, seconds: int) -> FastAPI:
    if not 1 <= seconds <= 1800:
        raise ValueError("demonstration must expire within 1800 seconds")
    settings = load_demo_settings(config)
    deadline = time.monotonic() + seconds

    def check() -> None:
        if time.monotonic() >= deadline or load_demo_settings(config) != settings:
            raise PermissionError("demonstration expired or configuration changed")

    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data_dir.is_symlink():
        raise ValueError("demonstration data directory cannot be a symlink")
    marker = data_dir / "demo-scope.json"
    if marker.exists():
        if json.loads(marker.read_text()) != {"version": 1, "scope": settings.scope}:
            raise ValueError("demonstration scope changed")
    elif any(data_dir.iterdir()):
        raise ValueError("a new empty demonstration data directory is required")
    else:
        marker.write_text(json.dumps({"version": 1, "scope": settings.scope}) + "\n")
    lock = (data_dir / "single-process.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ValueError("one demonstration process already owns this directory") from None
    demo = GuidedDemo(data_dir / "session", manifest)
    journal = DemoJournal(data_dir / "commands.sqlite")
    receiver = DemoReceiver(settings, journal, check)
    ingress = FeishuIngress(
        FeishuSettings(
            app_id=settings.app_id,
            encrypt_key=settings.encrypt_key,
            verification_token=settings.verification_token,
        ),
        settings.directory,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        worker: asyncio.Task[None] | None = None
        runtime = None
        try:
            check()
            await initialize_data(demo.config)
            runtime = await build_runtime(
                demo.config,
                trusted_actors=(campaign_admin(),),
                trusted_executors=(local_cli_executor(),),
            )
            async with httpx.AsyncClient(trust_env=False) as client:

                async def token(tenant_id: str) -> SecretStr:
                    del tenant_id
                    check()
                    response = await client.post(
                        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        json={
                            "app_id": settings.app_id,
                            "app_secret": settings.app_secret.get_secret_value(),
                        },
                        timeout=10,
                        follow_redirects=False,
                    )
                    response.raise_for_status()
                    result = response.json()
                    if result.get("code") != 0 or not result.get("tenant_access_token"):
                        raise ValueError("credential provisioning failed")
                    return SecretStr(result["tenant_access_token"])

                channel = FeishuChannel(
                    client,
                    token,
                    {
                        (campaign_admin().tenant_id, settings.chat_id): frozenset(
                            {campaign_admin().subject_id}
                        )
                    },
                )

                async def send(view: IMViewModel, page: int) -> SendResult:
                    check()
                    assert runtime is not None
                    ctx = runtime.new_context(
                        actor=campaign_admin(),
                        executor=local_cli_executor(),
                        session_id="guided-demo",
                        thread_id=demo.state.thread_id,
                        run_id="guided-demo",
                        correlation_id="guided-demo",
                    )
                    return await channel.send_view(
                        settings.chat_id, view, ctx, page=page, check_identity=check
                    )

                worker = asyncio.create_task(demo_worker(demo, journal, send, check))
                yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if runtime is not None:
                await runtime.aclose()
            lock.close()

    return create_demo_app(ingress, receiver, lifespan)
