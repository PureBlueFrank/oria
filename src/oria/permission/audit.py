"""Platform audit persistence with bounded field-level redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import EventEnvelope, JsonValue

_LOGGER = logging.getLogger(__name__)
_REDACTED = "[REDACTED]"
_SENSITIVE_FIELD_MARKERS = (
    "apikey",
    "argument",
    "authorization",
    "body",
    "chainofthought",
    "content",
    "cookie",
    "credential",
    "email",
    "fullname",
    "idcard",
    "input",
    "message",
    "mobile",
    "name",
    "password",
    "phone",
    "prompt",
    "query",
    "reasoning",
    "secret",
    "ssn",
    "subjectid",
    "token",
)


class AuditUnavailableError(RuntimeError):
    """Raised when a mandatory restricted-data audit record cannot be persisted."""


def _is_sensitive_field(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)


def _redact_value(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            {
                str(key): _REDACTED if _is_sensitive_field(str(key)) else _redact_value(item)
                for key, item in value.items()
            },
        )
    if isinstance(value, (list, tuple)):
        return cast(JsonValue, [_redact_value(item) for item in value])
    return cast(JsonValue, value)


def redact_audit_payload(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a detached payload with secret, prompt, reasoning, and PII fields redacted."""
    return {
        str(key): _REDACTED if _is_sensitive_field(str(key)) else _redact_value(value)
        for key, value in payload.items()
    }


class PlatformAuditService:
    """Append sanitized EventEnvelope records to the platform audit store."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: ResolvedRuntimeConfig,
    ) -> None:
        self._sessions = sessions
        self._fail_closed = config.environment == "production" and config.edition == "production"

    async def append(self, event: EventEnvelope, *, classification: str) -> bool:
        values = event.model_dump()
        values["payload"] = redact_audit_payload(event.payload)
        sanitized = EventEnvelope.model_validate(values)
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(event_id, occurred_at, tenant_id, actor, action, resource_type, "
                        "resource_id, resource_tenant_id, decision, policy_version, args_hash, "
                        "result, correlation_id, payload_json) VALUES "
                        "(:event_id, :occurred_at, :tenant_id, :actor, :action, :resource_type, "
                        ":resource_id, :resource_tenant_id, :decision, :policy_version, "
                        ":args_hash, :result, :correlation_id, :payload_json)"
                    ),
                    {
                        "event_id": sanitized.event_id,
                        "occurred_at": sanitized.occurred_at,
                        "tenant_id": sanitized.tenant_id,
                        "actor": sanitized.actor,
                        "action": sanitized.action,
                        "resource_type": sanitized.resource.resource_type,
                        "resource_id": sanitized.resource.resource_id,
                        "resource_tenant_id": sanitized.resource.tenant_id,
                        "decision": sanitized.decision,
                        "policy_version": sanitized.policy_version,
                        "args_hash": sanitized.args_hash,
                        "result": sanitized.result,
                        "correlation_id": sanitized.correlation_id,
                        "payload_json": json.dumps(
                            sanitized.payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                )
        except Exception as exc:
            if self._fail_closed and classification == "restricted":
                raise AuditUnavailableError(
                    "restricted operation audit persistence failed closed"
                ) from exc
            _LOGGER.warning("platform audit persistence unavailable; continuing in degraded mode")
            return False
        return True
