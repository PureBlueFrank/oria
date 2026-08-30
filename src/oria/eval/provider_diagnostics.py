"""Bounded, synthetic diagnostics for public Provider contract mismatches."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

import httpx
from pydantic import Field, SecretStr

from oria.core.types import ValueModel

_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)sk-[a-z0-9_-]{8,}"),
)

DeepSeekToolConclusion = Literal[
    "both_accepted",
    "adapter_input_shape_rejected",
    "official_string_shape_rejected",
    "provider_rejected_documented_payload",
    "inconclusive",
]


class DeepSeekToolProbeResult(ValueModel):
    probe_id: Literal["official_string_input", "oria_message_input"]
    outcome: Literal["tool_call", "rejected", "unexpected_response"]
    http_status: int = Field(ge=100, le=599)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str | None = None
    response_id: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    tool_name: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    error_param: str | None = None
    error_summary: str | None = Field(default=None, max_length=240)


class DeepSeekToolDiagnosticCard(ValueModel):
    schema_version: Literal[1] = 1
    target_id: Literal["deepseek"] = "deepseek"
    status: Literal["completed"] = "completed"
    model: str
    request_count: Literal[2] = 2
    conclusion: DeepSeekToolConclusion
    probes: tuple[DeepSeekToolProbeResult, DeepSeekToolProbeResult]


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _probe_payloads(model: str) -> tuple[tuple[str, dict[str, object]], ...]:
    tool: dict[str, object] = {
        "type": "function",
        "name": "oria_health_probe",
        "description": "Emit a no-argument provider health probe.",
        "parameters": {"type": "object", "properties": {}},
    }
    common: dict[str, object] = {
        "model": model,
        "tools": [tool],
        "tool_choice": "required",
        "reasoning": {"effort": "none"},
        "max_output_tokens": 512,
    }
    return (
        (
            "official_string_input",
            {**common, "input": "Call the oria_health_probe function once."},
        ),
        (
            "oria_message_input",
            {
                **common,
                "input": [
                    {
                        "role": "user",
                        "content": "Call the oria_health_probe function once.",
                    }
                ],
            },
        ),
    )


def _safe_string(value: object, *, secret: str, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if secret:
        normalized = normalized.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        normalized = pattern.sub("[REDACTED]", normalized)
    return normalized[:limit] or None


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _result_from_response(
    *,
    probe_id: Literal["official_string_input", "oria_message_input"],
    payload: dict[str, object],
    response: httpx.Response,
    secret: str,
) -> DeepSeekToolProbeResult:
    request_id = _safe_string(
        response.headers.get("x-request-id"),
        secret=secret,
        limit=128,
    )
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    typed_body = body if isinstance(body, dict) else {}
    payload_sha256 = _canonical_sha256(payload)
    if response.status_code >= 400:
        error = typed_body.get("error")
        typed_error = error if isinstance(error, dict) else {}
        return DeepSeekToolProbeResult(
            probe_id=probe_id,
            outcome="rejected",
            http_status=response.status_code,
            payload_sha256=payload_sha256,
            request_id=request_id,
            error_code=_safe_string(typed_error.get("code"), secret=secret, limit=80),
            error_type=_safe_string(typed_error.get("type"), secret=secret, limit=80),
            error_param=_safe_string(typed_error.get("param"), secret=secret, limit=120),
            error_summary=_safe_string(typed_error.get("message"), secret=secret, limit=240),
        )

    output = typed_body.get("output")
    output_items = output if isinstance(output, list) else []
    function_call = next(
        (
            item
            for item in output_items
            if isinstance(item, dict) and item.get("type") == "function_call"
        ),
        None,
    )
    usage = typed_body.get("usage")
    typed_usage = usage if isinstance(usage, dict) else {}
    tool_name = function_call.get("name") if isinstance(function_call, dict) else None
    return DeepSeekToolProbeResult(
        probe_id=probe_id,
        outcome="tool_call" if tool_name == "oria_health_probe" else "unexpected_response",
        http_status=response.status_code,
        payload_sha256=payload_sha256,
        request_id=request_id or _safe_string(typed_body.get("id"), secret=secret, limit=128),
        response_id=_safe_string(typed_body.get("id"), secret=secret, limit=128),
        model=_safe_string(typed_body.get("model"), secret=secret, limit=128),
        input_tokens=_optional_nonnegative_int(typed_usage.get("input_tokens")),
        output_tokens=_optional_nonnegative_int(typed_usage.get("output_tokens")),
        tool_name=_safe_string(tool_name, secret=secret, limit=128),
    )


async def diagnose_deepseek_responses_tools(
    *,
    client: httpx.AsyncClient,
    api_key: SecretStr,
    model: str,
) -> DeepSeekToolDiagnosticCard:
    """Compare documented string input with Oria's message-list input using synthetic data."""

    secret = api_key.get_secret_value()
    probes: list[DeepSeekToolProbeResult] = []
    for raw_probe_id, payload in _probe_payloads(model):
        probe_id: Literal["official_string_input", "oria_message_input"] = (
            "official_string_input"
            if raw_probe_id == "official_string_input"
            else "oria_message_input"
        )
        response = await client.post(
            "/responses",
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=60.0,
        )
        probes.append(
            _result_from_response(
                probe_id=probe_id,
                payload=payload,
                response=response,
                secret=secret,
            )
        )

    outcomes = tuple(probe.outcome == "tool_call" for probe in probes)
    conclusion: DeepSeekToolConclusion
    if outcomes == (True, True):
        conclusion = "both_accepted"
    elif outcomes == (True, False):
        conclusion = "adapter_input_shape_rejected"
    elif outcomes == (False, True):
        conclusion = "official_string_shape_rejected"
    elif all(probe.outcome == "rejected" for probe in probes):
        conclusion = "provider_rejected_documented_payload"
    else:
        conclusion = "inconclusive"
    return DeepSeekToolDiagnosticCard(
        model=model,
        conclusion=conclusion,
        probes=(probes[0], probes[1]),
    )
