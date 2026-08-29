"""V02-POL-01 security tests for ACL enforcement and platform auditing."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import (
    ACLMetadata,
    AuthorizationContext,
    AuthorizationRequest,
    EventEnvelope,
    QueryFilters,
    ResourceRef,
)
from oria.data import initialize_data
from oria.permission.audit import AuditUnavailableError, PlatformAuditService
from oria.permission.local import LOCAL_TENANT_ID, local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.storage.database import DatabaseResources

pytestmark = pytest.mark.security


def _event(payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": "audit-synthetic-denial",
            "occurred_at": datetime(2026, 8, 29, tzinfo=UTC),
            "tenant_id": LOCAL_TENANT_ID,
            "actor": "synthetic-subject",
            "action": "document:read",
            "resource": ResourceRef(
                resource_type="document",
                resource_id="synthetic-document",
                tenant_id=LOCAL_TENANT_ID,
            ),
            "decision": "deny",
            "policy_version": "synthetic-policy-v1",
            "args_hash": "sha256:" + "0" * 64,
            "result": "denied",
            "correlation_id": "synthetic-correlation",
            "payload": payload,
        }
    )


@pytest.mark.asyncio
async def test_document_acl_and_cross_tenant_denial_leave_sanitized_audit(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="acl-session",
            thread_id="acl-thread",
            run_id="acl-run",
        )
        restricted = demo_rule_document().model_copy(
            update={
                "document_id": "synthetic-acl-denied",
                "acl": ACLMetadata(
                    allowed_subject_ids=("other-synthetic-subject",),
                    classification="restricted",
                ),
            }
        )
        await ctx.knowledge.ingest(restricted, ctx)

        docs = await ctx.retriever.retrieve(
            "rules",
            ctx,
            k=10,
            query_filters=QueryFilters(attributes={"document_id": "synthetic-acl-denied"}),
        )
        cross_tenant = await runtime.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action="document:read",
                resource=ResourceRef(
                    resource_type="document",
                    resource_id="synthetic-cross-tenant",
                    tenant_id="other-tenant",
                ),
                context=AuthorizationContext(
                    correlation_id="synthetic-cross-correlation",
                    attributes={
                        "api_key": "synthetic-secret-value",
                        "prompt": "synthetic-full-prompt",
                        "chain_of_thought": "synthetic-hidden-reasoning",
                        "email": "synthetic@example.invalid",
                    },
                ),
            ),
            ctx,
        )

        assert docs == []
        assert cross_tenant.allow is False
    finally:
        await runtime.aclose()

    with sqlite3.connect(config.data_paths.platform_db) as connection:
        row = connection.execute(
            "SELECT tenant_id, decision, result, args_hash, payload_json "
            "FROM audit_events WHERE resource_id = ?",
            ("synthetic-cross-tenant",),
        ).fetchone()
    assert row is not None
    serialized = json.dumps(row, ensure_ascii=False)
    assert row[0:3] == (LOCAL_TENANT_ID, "deny", "denied")
    assert str(row[3]).startswith("sha256:")
    assert json.loads(str(row[4])) == {"reason_code": "cross_tenant"}
    for sensitive in (
        "synthetic-secret-value",
        "synthetic-full-prompt",
        "synthetic-hidden-reasoning",
        "synthetic@example.invalid",
    ):
        assert sensitive not in serialized


@pytest.mark.asyncio
async def test_audit_payload_redacts_secret_prompt_reasoning_and_unnecessary_pii(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    sensitive_values = (
        "synthetic-api-key",
        "synthetic-complete-prompt",
        "synthetic-private-reasoning",
        "synthetic-person@example.invalid",
        "synthetic-person-name",
        "synthetic-bearer-token",
    )
    event = _event(
        {
            "reason_code": "acl_denied",
            "api_key": sensitive_values[0],
            "prompt": sensitive_values[1],
            "chain_of_thought": sensitive_values[2],
            "email": sensitive_values[3],
            "customer_name": sensitive_values[4],
            "nested": {"authorization": sensitive_values[5]},
        }
    )

    async with DatabaseResources(config) as databases:
        audit = PlatformAuditService(databases.platform_sessions, config)
        assert await audit.append(event, classification="restricted") is True

    with sqlite3.connect(config.data_paths.platform_db) as connection:
        row = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
    assert row is not None
    payload_json = str(row[0])
    payload = json.loads(payload_json)
    assert payload["reason_code"] == "acl_denied"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["nested"]["authorization"] == "[REDACTED]"
    for sensitive in sensitive_values:
        assert sensitive not in payload_json


@pytest.mark.asyncio
async def test_restricted_production_audit_failure_fails_closed(tmp_path: Path) -> None:
    base = resolve_runtime_config(environ={}, data_dir=tmp_path / "missing-data")
    production = base.model_copy(update={"environment": "production", "edition": "production"})

    async with DatabaseResources(production) as databases:
        audit = PlatformAuditService(databases.platform_sessions, production)
        with pytest.raises(AuditUnavailableError, match="failed closed"):
            await audit.append(_event({"reason_code": "acl_denied"}), classification="restricted")
