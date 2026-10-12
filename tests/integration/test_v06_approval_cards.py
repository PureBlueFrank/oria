"""Persistent approval references remain scoped and never grant execution permission."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import text
from tests.contract.test_v06_feishu import SETTINGS, request
from tests.integration.test_v03_platform_migration import _config, _revision, _tables
from tests.security.test_v06_api_identity import binding

from oria.api.identity import AuthenticationError, IdentityDirectory
from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.ingress.approval_cards import ApprovalCardBindings
from oria.ingress.feishu import FeishuIngress
from oria.migrations.runner import upgrade_databases
from oria.permission.audit import PlatformAuditService
from oria.permission.local import LocalPolicyEngine
from oria.storage.database import DatabaseResources
from oria.storage.platform import SQLiteApprovalRepository

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_binding_migration_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "platform.db"
    cfg = _config(path)
    command.upgrade(cfg, "platform_0010")
    before = _tables(path)
    command.upgrade(cfg, "head")
    assert _revision(path) == "platform_0011"
    assert _tables(path) - before == {"approval_card_bindings"}
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(approval_card_bindings)")
        }
        assert columns["decision_intent"][3] == 1
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'approval_card_bindings'"
        ).fetchone()[0]
        assert "decision_intent" in sql and "approve" in sql
    command.downgrade(cfg, "platform_0010")
    assert _tables(path) == before


@pytest.mark.asyncio
async def test_persistent_reference_adversarial_resolution(tmp_path):
    identity = binding(issuer="feishu:app:ft", subject="ou")
    directory = IdentityDirectory(bindings=(identity,))
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    db = DatabaseResources(config)
    policy = LocalPolicyEngine(
        PlatformAuditService(db.platform_sessions, config),
        trusted_actors=(identity.actor,),
        trusted_executors=(identity.executor,),
    )
    # The context is lightweight; Policy, authorization audit and SQLite are real.
    runtime = SimpleNamespace(
        policy=policy,
        new_context=lambda **kw: SimpleNamespace(
            **kw,
            tenant_id=kw["actor"].tenant_id,
        ),
    )
    now = datetime.now(UTC)
    approval = Approval(
        approval_id="card",
        tenant_id="tenant",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash="sha256:" + "a" * 64,
        checkpoint_id="checkpoint",
        policy_version="local-v2",
        requester="requester",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )
    async with db:
        repository = SQLiteApprovalRepository(db.platform_sessions)
        await repository.add(approval)
        service = ApprovalCardBindings(runtime, repository, lambda: directory, db.platform_sessions)
        action = await service.issue("card", identity, chat_id="chat", message_id="msg")
        async with db.platform_sessions() as session:
            row = (
                (await session.execute(text("SELECT * FROM approval_card_bindings")))
                .mappings()
                .one()
            )
            assert action.action_ref not in str(dict(row))
            assert "checkpoint" not in str(dict(row))
            assert row["decision_intent"] == "approve"
        with pytest.raises(ValueError, match="unsupported"):
            await service.issue(
                "card",
                identity,
                chat_id="chat",
                message_id="msg",
                decision_intent="reject",  # type: ignore[arg-type]
            )
    # Reopen connections and service: the binding must survive process-resource restart.
    async with DatabaseResources(config) as db:
        policy = LocalPolicyEngine(
            PlatformAuditService(db.platform_sessions, config),
            trusted_actors=(identity.actor,),
            trusted_executors=(identity.executor,),
        )
        runtime.policy = policy
        repository = SQLiteApprovalRepository(db.platform_sessions)
        service = ApprovalCardBindings(runtime, repository, lambda: directory, db.platform_sessions)
        document = {
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
                "context": {"open_chat_id": "chat", "open_message_id": "msg"},
            },
        }
        event = FeishuIngress(SETTINGS, lambda: directory).verify(request(document))
        assert await service.resolve(event) == approval
        assert await service.resolve(event) == approval
        # Authorization is audited, without deciding or accepting a business action.
        assert (await repository.get("tenant", "card")).status == "pending"
        async with db.platform_sessions() as session:
            counts = {
                table: (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                for table in (
                    "audit_events",
                    "ingress_receipts",
                    "ingress_replays",
                    "outbox",
                )
            }
            assert counts == {
                "audit_events": 3,
                "ingress_receipts": 0,
                "ingress_replays": 0,
                "outbox": 0,
            }
            binding_rows = (
                (await session.execute(text("SELECT * FROM approval_card_bindings")))
                .mappings()
                .all()
            )
            assert len(binding_rows) == 1
        for field, value in [
            ("target_ref", "other-chat"),
            ("source_message_id", "other-message"),
            ("mapped_tenant_id", "foreign"),
            ("mapped_subject_id", "other"),
            ("sender_ref", "other"),
        ]:
            wrong = event.model_copy(
                update={"message": event.message.model_copy(update={field: value})}
            )
            with pytest.raises(PermissionError):
                await service.resolve(wrong)
        wrong = event.model_copy(
            update={"action": action.model_copy(update={"action_ref": "unknown"})}
        )
        with pytest.raises(PermissionError):
            await service.resolve(wrong)
        expired = event.model_copy(update={"expires_at": now.timestamp() - 1})
        with pytest.raises(PermissionError):
            await service.resolve(expired)
        directory = IdentityDirectory()
        with pytest.raises(AuthenticationError):
            await service.resolve(event)
        directory = IdentityDirectory(bindings=(identity,))
        # Even an unchanged opaque reference becomes stale when the stored record changes.
        changed = approval.model_copy(update={"updated_at": now + timedelta(seconds=1)})
        await repository.replace(changed, expected=approval)
        with pytest.raises(PermissionError):
            await service.resolve(event)
        own = approval.model_copy(
            update={"approval_id": "own", "requester": identity.actor.subject_id}
        )
        await repository.add(own)
        with pytest.raises(PermissionError):
            await service.issue("own", identity, chat_id="chat", message_id="msg")
        with pytest.raises(PermissionError):
            await service.issue("missing", identity, chat_id="chat", message_id="msg")
