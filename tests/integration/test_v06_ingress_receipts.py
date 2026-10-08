"""Transactional ingress receipts: durable duplicates, rollback, and no body storage."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from tests.contract.test_v06_feishu import event, ingress, request
from tests.integration.test_v03_platform_migration import _config, _revision, _tables

from oria.api.identity import IdentityDirectory
from oria.config import resolve_runtime_config
from oria.ingress.feishu import CardAction
from oria.ingress.receipts import IngressConflict, TransactionalFeishuReceiver
from oria.migrations.runner import upgrade_databases
from oria.storage.database import DatabaseResources

pytestmark = [pytest.mark.integration, pytest.mark.security]


class Acceptor:
    """Synthetic business insert into the very same transaction, not a real Job."""

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.denied = False

    async def authorize(self, session, event):
        if self.denied:
            raise PermissionError("denied")

    async def persist(self, session, event, receipt_ref):
        self.calls += 1
        await session.execute(
            text("INSERT INTO receipt_test_business VALUES (:ref)"), {"ref": receipt_ref}
        )
        if self.fail:
            raise RuntimeError("synthetic transaction failure")


def test_upgrade_from_previous_head_and_rollback(tmp_path):
    path = tmp_path / "platform.db"
    cfg = _config(path)
    command.upgrade(cfg, "platform_0009")
    before = _tables(path)
    command.upgrade(cfg, "head")
    assert _revision(path) == "platform_0010"
    assert _tables(path) - before == {"ingress_receipts", "ingress_replays"}
    command.downgrade(cfg, "platform_0009")
    assert _tables(path) == before


@pytest.mark.asyncio
async def test_receipts_restart_duplicate_conflict_rollback_and_revocation(tmp_path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    value = ingress().verify(request(event()))
    directory = IdentityDirectory(bindings=(value.binding,))
    acceptor = Acceptor()
    async with DatabaseResources(config) as db:
        async with db.platform_sessions.begin() as session:
            await session.execute(
                text("CREATE TABLE receipt_test_business (ref VARCHAR PRIMARY KEY)")
            )
        receiver = TransactionalFeishuReceiver(db.platform_sessions, lambda: directory, acceptor)
        await asyncio.gather(receiver.accept(value), receiver.accept(value))
        assert acceptor.calls == 1
        changed = value.model_copy(
            update={"message": value.message.model_copy(update={"text": "tampered"})}
        )
        with pytest.raises(IngressConflict):
            await receiver.accept(changed)
        changed_nonce = value.model_copy(update={"replay_key": "fresh-nonce"})
        await receiver.accept(changed_nonce)
        assert acceptor.calls == 1
        other = value.model_copy(
            update={"message": value.message.model_copy(update={"dedupe_key": "other-source"})}
        )
        with pytest.raises(IngressConflict):
            await receiver.accept(other)
        # A separately delivered source can retry after a failed transaction.
        second = other.model_copy(update={"replay_key": "second-nonce"})
        # Kill an independent connection before commit; its receipt/business writes vanish.
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute("INSERT INTO ingress_receipts VALUES "
                   "('tenant','other-source','hash','crash','2026-10-05')")
connection.execute("INSERT INTO receipt_test_business VALUES ('crash')")
os._exit(12)
""",
                str(config.data_paths.platform_db),
            ],
            check=False,
            timeout=10,
        )
        assert crashed.returncode == 12
        acceptor.fail = True
        with pytest.raises(RuntimeError, match="synthetic"):
            await receiver.accept(second)
        async with db.platform_sessions() as session:
            assert (
                await session.execute(text("SELECT count(*) FROM ingress_receipts"))
            ).scalar_one() == 1
            assert (
                await session.execute(text("SELECT count(*) FROM receipt_test_business"))
            ).scalar_one() == 1
        acceptor.fail = False
        await receiver.accept(second)
        acceptor.denied = True
        with pytest.raises(PermissionError):
            await receiver.accept(value)
        acceptor.denied = False
        directory = IdentityDirectory()
        with pytest.raises(PermissionError):
            await receiver.accept(value)
        directory = IdentityDirectory(bindings=(value.binding,))
        expired = value.model_copy(update={"expires_at": datetime.now(UTC).timestamp() - 1})
        with pytest.raises(PermissionError):
            await receiver.accept(expired)
    # New engine and receiver reuse committed receipts after process-resource restart.
    restarted = Acceptor()
    async with DatabaseResources(config) as db:
        await TransactionalFeishuReceiver(
            db.platform_sessions, lambda: directory, restarted
        ).accept(value)
    assert restarted.calls == 0
    with sqlite3.connect(config.data_paths.platform_db) as connection:
        rows = connection.execute("SELECT * FROM ingress_receipts").fetchall()
        assert len(rows) == 2
        assert "查询活动" not in str(rows) and "fixture-token" not in str(rows)
        assert "actor" not in str(rows) and "raw_body" not in str(rows)


@pytest.mark.asyncio
async def test_action_duplicates_are_tenant_scoped_and_policy_is_rechecked(tmp_path):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    upgrade_databases(config)
    message = ingress().verify(request(event()))
    action = message.model_copy(
        update={"kind": "action", "action": CardAction(action_ref="decision-1")}
    )
    other_binding = action.binding.model_copy(
        update={
            "issuer": "feishu:app:other",
            "actor": action.binding.actor.model_copy(update={"tenant_id": "other"}),
            "executor": action.binding.executor.model_copy(update={"tenant_id": "other"}),
        }
    )
    other = action.model_copy(
        update={
            "binding": other_binding,
            "message": action.message.model_copy(update={"mapped_tenant_id": "other"}),
        }
    )
    directory = IdentityDirectory(bindings=(action.binding, other.binding))
    acceptor = Acceptor()
    async with DatabaseResources(config) as db:
        async with db.platform_sessions.begin() as session:
            await session.execute(
                text("CREATE TABLE receipt_test_business (ref VARCHAR PRIMARY KEY)")
            )
        receiver = TransactionalFeishuReceiver(db.platform_sessions, lambda: directory, acceptor)
        await receiver.accept(action)
        await receiver.accept(action)
        await receiver.accept(other)
        assert acceptor.calls == 2
        acceptor.denied = True
        with pytest.raises(PermissionError):
            await receiver.accept(action)
        async with db.platform_sessions() as session:
            assert (
                await session.execute(text("SELECT count(*) FROM ingress_receipts"))
            ).scalar_one() == 2
