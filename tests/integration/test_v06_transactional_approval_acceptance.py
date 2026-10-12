"""Transactional Feishu approval acceptance over real SQLite repositories."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from tests.contract.test_v06_feishu import SETTINGS, request
from tests.security.test_v06_api_identity import binding

from oria.api.app import create_app
from oria.api.approvals import ApprovalAPI, approval_revision
from oria.api.identity import IdentityBinding, IdentityDirectory, JWTAuthenticator, JWTSettings
from oria.config import resolve_runtime_config
from oria.core.approvals import Approval, ApprovalConflict
from oria.core.runtime import RuntimeServices, build_runtime
from oria.ingress.approval_cards import ApprovalCardBindings, TransactionalApprovalCardAcceptor
from oria.ingress.feishu import CardAction, FeishuIngress, VerifiedFeishuEvent
from oria.ingress.receipts import IngressUnavailable, TransactionalFeishuReceiver
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _runtime(
    tmp_path,
    identity: IdentityBinding,
    *,
    clock: Callable[[], datetime],
) -> tuple[RuntimeServices, DatabaseResources, SQLiteApprovalRepository]:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    databases = DatabaseResources(config)
    runtime = await build_runtime(
        config,
        resource_factories=(lambda: databases,),
        clock=clock,
        trusted_actors=(identity.actor,),
        trusted_executors=(identity.executor,),
    )
    return runtime, databases, SQLiteApprovalRepository(databases.platform_sessions)


def _document(action: CardAction, *, chat_id: str = "chat", message_id: str = "card-msg"):
    return {
        "schema": "2.0",
        "header": {
            "app_id": "app",
            "tenant_key": "ft",
            "token": "fixture-token",
            "event_id": "evt",
            "event_type": "card.action.trigger",
        },
        "event": {
            "operator": {"tenant_key": "ft", "open_id": "ou"},
            "action": {"value": action.model_dump()},
            "context": {"open_chat_id": chat_id, "open_message_id": message_id},
        },
    }


def _event(
    action: CardAction,
    directory: Callable[[], IdentityDirectory],
    *,
    nonce: str = "approval-nonce",
) -> tuple[VerifiedFeishuEvent, object]:
    inbound = request(_document(action), nonce=nonce)
    verified = FeishuIngress(SETTINGS, directory).verify(inbound)
    assert isinstance(verified, VerifiedFeishuEvent)
    return verified, inbound


async def _counts(databases: DatabaseResources) -> dict[str, int]:
    async with databases.platform_sessions() as session:
        return {
            table: int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
            for table in (
                "ingress_receipts",
                "ingress_replays",
                "audit_events",
                "outbox",
                "approval_card_bindings",
            )
        }


async def _approval(
    repository: SQLiteApprovalRepository,
    now: datetime,
    *,
    approval_id: str = "approval-card",
    policy_version: str = "local-v2",
) -> Approval:
    value = Approval(
        approval_id=approval_id,
        tenant_id="tenant",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash="sha256:" + "a" * 64,
        checkpoint_id="checkpoint-secret",
        policy_version=policy_version,
        requester="requester",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )
    await repository.add(value)
    return value


def _authenticator() -> JWTAuthenticator:
    """Uncalled JWT stub; this suite exercises the signed Feishu callback branch."""

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    return JWTAuthenticator(
        JWTSettings(
            issuer="https://identity.invalid",
            audience="oria",
            jwks_url="https://identity.invalid/keys",
        ),
        IdentityDirectory,
        client,
    )


@pytest.mark.asyncio
async def test_signed_http_success_duplicate_and_restart_are_exactly_once(tmp_path):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path, identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id,
            identity,
            chat_id="chat",
            message_id="card-msg",
        )
        issued_counts = await _counts(databases)
        resolution_event, _ = _event(action, source)
        assert await cards.resolve(resolution_event) == original
        assert await cards.resolve(resolution_event) == original
        baseline = await _counts(databases)
        expected_resolution_counts = dict(issued_counts)
        expected_resolution_counts["audit_events"] += 2
        assert baseline == expected_resolution_counts
        verified, inbound = _event(action, source)
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        auth = _authenticator()
        app = create_app(
            authenticator=auth,
            feishu=FeishuIngress(SETTINGS, source),
            receiver=receiver,
            close=auth.client.aclose,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/ingress/feishu/events",
                content=inbound.raw_body,
                headers=dict(inbound.headers),
            )
            assert response.status_code == 200
            duplicate = await client.post(
                "/v1/ingress/feishu/events",
                content=inbound.raw_body,
                headers=dict(inbound.headers),
            )
            assert duplicate.status_code == 200
        await auth.client.aclose()
        counts = await _counts(databases)
        assert counts["ingress_receipts"] == counts["ingress_replays"] == 1
        assert counts["audit_events"] == baseline["audit_events"] + 1
        assert counts["outbox"] == baseline["outbox"] + 1
        assert counts["approval_card_bindings"] == 1
        decided = await repository.get("tenant", original.approval_id)
        assert decided is not None and decided.status == "approved"
        async with databases.platform_sessions() as session:
            receipt_ref = (
                await session.execute(text("SELECT receipt_ref FROM ingress_receipts"))
            ).scalar_one()
            audit = (
                await session.execute(
                    text(
                        "SELECT correlation_id, action FROM audit_events "
                        "WHERE action = 'approval.decided'"
                    )
                )
            ).one()
            assert audit == (receipt_ref, "approval.decided")
            persisted = str(
                (
                    await session.execute(
                        text("SELECT * FROM ingress_receipts, audit_events, outbox")
                    )
                ).all()
            )
            assert action.action_ref not in persisted
            assert "checkpoint-secret" not in persisted
        await receiver.accept(verified)
        committed_counts = await _counts(databases)

        # A committed retry ignores the old pending revision, but rechecks every
        # current authorization boundary and never creates another side effect.
        directory = IdentityDirectory()
        with pytest.raises(PermissionError):
            await receiver.accept(verified)
        directory = IdentityDirectory(bindings=(identity,))
        changed_identity = identity.model_copy(
            update={"actor": identity.actor.model_copy(update={"roles": ("operator",)})}
        )
        directory = IdentityDirectory(bindings=(changed_identity,))
        changed_event, _ = _event(action, source, nonce="duplicate-role-change")
        with pytest.raises(PermissionError):
            await receiver.accept(changed_event)
        committed_counts["audit_events"] += 1
        directory = IdentityDirectory(bindings=(identity,))
        async with databases.platform_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE approvals SET requester = :requester, decider = 'other-decider' "
                    "WHERE approval_id = :id"
                ),
                {"requester": identity.actor.subject_id, "id": original.approval_id},
            )
        with pytest.raises(PermissionError):
            await receiver.accept(verified)
        async with databases.platform_sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE approvals SET requester = :requester, decider = :decider "
                    "WHERE approval_id = :id"
                ),
                {
                    "requester": original.requester,
                    "decider": identity.actor.subject_id,
                    "id": original.approval_id,
                },
            )
            await session.execute(
                text("UPDATE approval_card_bindings SET expires_at = :expires"),
                {"expires": now.timestamp() - 1},
            )
        with pytest.raises(PermissionError):
            await receiver.accept(verified)
        async with databases.platform_sessions.begin() as session:
            await session.execute(
                text("UPDATE approval_card_bindings SET expires_at = :expires"),
                {"expires": original.expires_at.timestamp()},
            )
        assert await _counts(databases) == committed_counts
    finally:
        await runtime.aclose()

    restarted_runtime, restarted_db, restarted_repository = await _runtime(
        tmp_path, identity, clock=clock
    )
    try:
        restarted = TransactionalFeishuReceiver(
            restarted_db.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(
                restarted_runtime,
                restarted_repository,
                source,
                clock=clock,
            ),
        )
        await restarted.accept(verified)
        restarted_counts = await _counts(restarted_db)
        assert restarted_counts["audit_events"] == committed_counts["audit_events"]
        assert restarted_counts["outbox"] == counts["outbox"]
    finally:
        await restarted_runtime.aclose()


class _FailAfterAcceptor:
    """Fault-injection wrapper only; production acceptors keep no per-event state."""

    def __init__(self, delegate: TransactionalApprovalCardAcceptor) -> None:
        self._delegate = delegate

    async def accept(self, session, event, receipt_ref, *, duplicate):
        outcome = await self._delegate.accept(session, event, receipt_ref, duplicate=duplicate)
        if not duplicate:
            raise RuntimeError("synthetic failure after outbox")
        return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["cas", "audit", "outbox"])
async def test_failure_after_each_decision_write_rolls_back_and_retries(tmp_path, stage):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path / stage, identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        baseline = await _counts(databases)
        verified, _ = _event(action, source, nonce=f"fault-{stage}")
        acceptor = TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock)
        if stage in {"cas", "audit"}:
            target = "approvals" if stage == "cas" else "outbox"
            timing = "AFTER UPDATE OF status" if stage == "cas" else "BEFORE INSERT"
            async with databases.platform_sessions.begin() as session:
                await session.execute(
                    text(
                        f"CREATE TRIGGER fail_{stage} {timing} ON {target} "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
                    )
                )
            failing = acceptor
        else:
            failing = _FailAfterAcceptor(acceptor)
        receiver = TransactionalFeishuReceiver(databases.platform_sessions, source, failing)
        with pytest.raises((RuntimeError, IngressUnavailable, ApprovalConflict)):
            await receiver.accept(verified)
        assert await repository.get("tenant", original.approval_id) == original
        counts = await _counts(databases)
        assert counts["ingress_receipts"] == counts["ingress_replays"] == 0
        assert counts["audit_events"] == baseline["audit_events"]
        assert counts["outbox"] == baseline["outbox"]
        assert counts["approval_card_bindings"] == 1
        if stage in {"cas", "audit"}:
            async with databases.platform_sessions.begin() as session:
                await session.execute(text(f"DROP TRIGGER fail_{stage}"))
        await TransactionalFeishuReceiver(databases.platform_sessions, source, acceptor).accept(
            verified
        )
        assert (await repository.get("tenant", original.approval_id)).status == "approved"  # type: ignore[union-attr]
        assert (await _counts(databases))["audit_events"] == baseline["audit_events"] + 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_two_refs_and_http_card_cas(tmp_path):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path / "duplicate", identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        baseline = await _counts(databases)
        verified, _ = _event(action, source)
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        await asyncio.gather(receiver.accept(verified), receiver.accept(verified))
        assert (await _counts(databases))["audit_events"] == baseline["audit_events"] + 1
    finally:
        await runtime.aclose()

    runtime, databases, repository = await _runtime(tmp_path / "two-refs", identity, clock=clock)
    try:
        original = await _approval(repository, now, approval_id="two-refs")
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        first, second = await asyncio.gather(
            cards.issue(original.approval_id, identity, chat_id="chat", message_id="card-msg"),
            cards.issue(original.approval_id, identity, chat_id="chat", message_id="card-msg"),
        )
        baseline = await _counts(databases)
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        results = await asyncio.gather(
            receiver.accept(_event(first, source, nonce="first")[0]),
            receiver.accept(_event(second, source, nonce="second")[0]),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        counts = await _counts(databases)
        assert counts["ingress_receipts"] == 1
        assert counts["audit_events"] == baseline["audit_events"] + 1
        assert counts["outbox"] == baseline["outbox"] + 1
    finally:
        await runtime.aclose()

    runtime, databases, repository = await _runtime(tmp_path / "http-card", identity, clock=clock)
    try:
        original = await _approval(repository, now, approval_id="http-card")
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        _, inbound = _event(action, source, nonce="http-card")
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        card_auth = _authenticator()
        card_app = create_app(
            authenticator=card_auth,
            feishu=FeishuIngress(SETTINGS, source),
            receiver=receiver,
            close=card_auth.client.aclose,
        )
        http_app = create_app(
            local_identity=identity,
            approvals=ApprovalAPI(runtime, repository, source),
        )
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=card_app), base_url="http://test"
            ) as card_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=http_app), base_url="http://127.0.0.1"
            ) as approval_client,
        ):
            card_response, http_response = await asyncio.gather(
                card_client.post(
                    "/v1/ingress/feishu/events",
                    content=inbound.raw_body,
                    headers=dict(inbound.headers),
                ),
                approval_client.post(
                    f"/v1/approvals/{original.approval_id}/decision",
                    json={
                        "decision": "approve",
                        "expected_revision": approval_revision(original),
                    },
                ),
            )
        assert sorted((card_response.status_code, http_response.status_code)) == [200, 409]
        await card_auth.client.aclose()
        async with databases.platform_sessions() as session:
            assert (
                await session.execute(
                    text("SELECT count(*) FROM audit_events WHERE action = 'approval.decided'")
                )
            ).scalar_one() == 1
            assert (await session.execute(text("SELECT count(*) FROM outbox"))).scalar_one() == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_version_invalidates_without_accepting_new_action(tmp_path):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path, identity, clock=clock)
    try:
        original = await _approval(repository, now, policy_version="old-policy")
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        baseline = await _counts(databases)
        verified, _ = _event(action, source)
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        with pytest.raises(PermissionError):
            await receiver.accept(verified)
        current = await repository.get("tenant", original.approval_id)
        assert current is not None and current.status == "invalidated"
        counts = await _counts(databases)
        assert counts["ingress_receipts"] == counts["ingress_replays"] == 0
        assert counts["audit_events"] == baseline["audit_events"] + 1
        assert counts["outbox"] == baseline["outbox"] + 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown-ref",
        "revision",
        "revocation",
        "sod",
        "tenant",
        "chat",
        "message",
        "sender",
        "callback-expiry",
        "binding-expiry",
        "approval-expiry",
        "role-change",
    ],
)
async def test_rejected_or_tampered_actions_leave_no_acceptance_writes(tmp_path, case):
    now = datetime.now(UTC)
    clock = Clock(now)
    issued_identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(issued_identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path / case, issued_identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, issued_identity, chat_id="chat", message_id="card-msg"
        )
        baseline = await _counts(databases)
        event, _ = _event(action, source)
        if case == "unknown-ref":
            event = event.model_copy(update={"action": CardAction(action_ref="unknown")})
        elif case == "revision":
            changed = original.model_copy(update={"updated_at": now + timedelta(seconds=1)})
            await repository.replace(changed, expected=original)
        elif case == "revocation":
            directory = IdentityDirectory()
        elif case == "sod":
            changed = original.model_copy(update={"requester": issued_identity.actor.subject_id})
            async with databases.platform_sessions.begin() as session:
                await session.execute(
                    text("UPDATE approvals SET requester = :requester WHERE approval_id = :id"),
                    {"requester": changed.requester, "id": changed.approval_id},
                )
                await session.execute(
                    text("UPDATE approval_card_bindings SET approval_revision = :revision"),
                    {"revision": approval_revision(changed)},
                )
        elif case in {"tenant", "chat", "message", "sender"}:
            field, value = {
                "tenant": ("mapped_tenant_id", "other"),
                "chat": ("target_ref", "other"),
                "message": ("source_message_id", "other"),
                "sender": ("sender_ref", "other"),
            }[case]
            event = event.model_copy(
                update={"message": event.message.model_copy(update={field: value})}
            )
        elif case == "callback-expiry":
            event = event.model_copy(update={"expires_at": now.timestamp() - 1})
        elif case == "binding-expiry":
            async with databases.platform_sessions.begin() as session:
                await session.execute(
                    text("UPDATE approval_card_bindings SET expires_at = :expires"),
                    {"expires": now.timestamp() - 1},
                )
        elif case == "approval-expiry":
            async with databases.platform_sessions.begin() as session:
                await session.execute(
                    text("UPDATE approval_card_bindings SET expires_at = :expires"),
                    {"expires": (now + timedelta(hours=3)).timestamp()},
                )
            clock.now = now + timedelta(hours=2)
            event = event.model_copy(update={"expires_at": (now + timedelta(hours=3)).timestamp()})
        elif case == "role-change":
            changed_actor = issued_identity.actor.model_copy(update={"roles": ("operator",)})
            changed_identity = issued_identity.model_copy(update={"actor": changed_actor})
            directory = IdentityDirectory(bindings=(changed_identity,))
            event, _ = _event(action, source, nonce="role-change")
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        with pytest.raises((PermissionError, ValueError)):
            await receiver.accept(event)
        counts = await _counts(databases)
        assert counts["ingress_receipts"] == counts["ingress_replays"] == 0
        assert counts["approval_card_bindings"] == 1
        current = await repository.get("tenant", original.approval_id)
        if case == "approval-expiry":
            assert current is not None and current.status == "expired"
            assert counts["audit_events"] == baseline["audit_events"] + 1
            assert counts["outbox"] == baseline["outbox"] + 1
        else:
            assert current is not None and current.status == "pending"
            assert counts["audit_events"] == baseline["audit_events"] + (case == "role-change")
            assert counts["outbox"] == baseline["outbox"]
    finally:
        await runtime.aclose()


def test_migration_rejects_non_approve_intent(tmp_path):
    import sqlite3

    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    with (
        sqlite3.connect(config.data_paths.platform_db) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO approval_card_bindings VALUES "
            "('tenant', 'hash', 'approval', 'revision', 'reject', "
            "'issuer', 'subject', 'actor', 'chat', 'message', 1)"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "after-business"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_revocation_during_acceptance_rolls_back_before_commit(tmp_path, stage, duplicate):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path, identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        event, _ = _event(action, source)
        if duplicate:
            await TransactionalFeishuReceiver(
                databases.platform_sessions,
                source,
                TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
            ).accept(event)
            event, _ = _event(action, source, nonce="revoked-duplicate")
        before = await repository.get("tenant", original.approval_id)
        baseline = await _counts(databases)

        class RevokingRepository(SQLiteApprovalRepository):
            async def _get(self, session, tenant_id, approval_id):
                nonlocal directory
                result = await super()._get(session, tenant_id, approval_id)
                directory = IdentityDirectory()
                return result

        owner = RevokingRepository(databases.platform_sessions) if stage == "read" else repository
        delegate = TransactionalApprovalCardAcceptor(runtime, owner, source, clock=clock)

        class RevokingAcceptor:
            async def accept(self, session, event, receipt_ref, *, duplicate):
                nonlocal directory
                outcome = await delegate.accept(session, event, receipt_ref, duplicate=duplicate)
                directory = IdentityDirectory()
                return outcome

        acceptor = delegate if stage == "read" else RevokingAcceptor()
        receiver = TransactionalFeishuReceiver(databases.platform_sessions, source, acceptor)
        with pytest.raises(PermissionError):
            await receiver.accept(event)
        assert await repository.get("tenant", original.approval_id) == before
        assert await _counts(databases) == baseline
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_policy_denial_is_audited_after_acceptance_rollback(tmp_path, duplicate):
    now = datetime.now(UTC)
    clock = Clock(now)
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))

    def source():
        return directory

    runtime, databases, repository = await _runtime(tmp_path, identity, clock=clock)
    try:
        original = await _approval(repository, now)
        cards = ApprovalCardBindings(runtime, repository, source, databases.platform_sessions)
        action = await cards.issue(
            original.approval_id, identity, chat_id="chat", message_id="card-msg"
        )
        receiver = TransactionalFeishuReceiver(
            databases.platform_sessions,
            source,
            TransactionalApprovalCardAcceptor(runtime, repository, source, clock=clock),
        )
        if duplicate:
            await receiver.accept(_event(action, source)[0])
        before = await repository.get("tenant", original.approval_id)
        baseline = await _counts(databases)
        changed = identity.model_copy(
            update={"actor": identity.actor.model_copy(update={"roles": ("operator",)})}
        )
        directory = IdentityDirectory(bindings=(changed,))
        event, _ = _event(action, source, nonce="denied-action")
        with pytest.raises(PermissionError):
            await asyncio.wait_for(receiver.accept(event), timeout=5)
        expected_counts = dict(baseline)
        expected_counts["audit_events"] += 1
        assert await _counts(databases) == expected_counts
        assert await repository.get("tenant", original.approval_id) == before
        async with databases.platform_sessions() as session:
            audit = (
                await session.execute(
                    text(
                        "SELECT action, decision, result, payload_json FROM audit_events "
                        "WHERE decision = 'deny'"
                    )
                )
            ).one()
            assert audit.action == "approval:launch:decide"
            assert audit.result == "denied"
            assert action.action_ref not in audit.payload_json
            assert "checkpoint-secret" not in audit.payload_json
    finally:
        await runtime.aclose()
