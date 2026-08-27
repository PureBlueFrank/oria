"""Adversarial Provider error, schema, and disclosure boundaries for T04."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from oria.config import ConfigResolutionError, resolve_runtime_config
from oria.config.models import ResolvedLLMProfile
from oria.core.runtime import build_runtime
from oria.core.types import ChatOptions, Message, ResponseSchema
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.errors import (
    AuthenticationError,
    ProviderTimeoutError,
    RateLimitError,
)
from oria.providers.openai_compat import OpenAICompatProvider, _diagnostic_payload

pytestmark = pytest.mark.security


def _profile() -> ResolvedLLMProfile:
    return ResolvedLLMProfile.model_validate(
        {
            "profile_id": "deepseek",
            "provider": "deepseek",
            "api_dialect": "responses",
            "model": "deepseek-v4-flash",
            "api_key": "secret-provider-key",
            "base_url": "https://api.deepseek.com",
            "structured_output_mode": "native_json_schema",
        }
    )


async def _context(tmp_path: Path):
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=tmp_path / "data"))
    return runtime, runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="security-session",
        thread_id="security-thread",
        run_id="security-run",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "__oria_submit_response__", "json_schema": {"type": "object"}},
        {"name": "invalid name", "json_schema": {"type": "object"}},
        {"name": "valid_name", "json_schema": {"type": "not-a-json-schema-type"}},
        {"name": "valid_name", "json_schema": {"type": "array"}},
    ],
)
def test_response_schema_rejects_reserved_invalid_or_non_object_contracts(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ResponseSchema.model_validate(payload)


@pytest.mark.asyncio
async def test_http_auth_and_rate_limit_errors_are_safe_and_typed(tmp_path: Path) -> None:
    responses = iter(
        [
            httpx.Response(401, text="secret upstream auth detail"),
            httpx.Response(429, text="private rate limit detail", headers={"retry-after": "3"}),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    runtime, ctx = await _context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            provider = OpenAICompatProvider(_profile(), client)
            with pytest.raises(AuthenticationError) as auth:
                await provider.chat([Message(role="user", content="auth")], ctx)
            with pytest.raises(RateLimitError) as limited:
                await provider.chat([Message(role="user", content="rate")], ctx)
        finally:
            await runtime.aclose()

    assert auth.value.retryable is False
    assert "secret upstream" not in str(auth.value)
    assert limited.value.retryable is True
    assert limited.value.retry_after == 3.0
    assert "private rate" not in str(limited.value)


@pytest.mark.asyncio
async def test_http_timeout_is_typed_without_internal_details(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private socket coordinates", request=request)

    runtime, ctx = await _context(tmp_path)
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            with pytest.raises(ProviderTimeoutError) as excinfo:
                await OpenAICompatProvider(_profile(), client).chat(
                    [Message(role="user", content="timeout")],
                    ctx,
                    options=ChatOptions(timeout_seconds=0.1),
                )
        finally:
            await runtime.aclose()
    assert excinfo.value.retryable is True
    assert "socket" not in str(excinfo.value)


def test_bge_profile_requires_revision_and_forbids_remote_code(tmp_path: Path) -> None:
    config = tmp_path / "invalid-bge.yaml"
    config.write_text(
        """\
environment: test
runtime_profile: standard
embedding:
  active_profile: bge
  profiles:
    bge:
      revision: null
      trust_remote_code: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigResolutionError, match=r"revision|remote code"):
        resolve_runtime_config(config_path=config, environ={}, cwd=tmp_path)


def test_diagnostic_payload_redacts_common_credential_key_variants() -> None:
    diagnostic = _diagnostic_payload(
        {
            "apiKey": "secret-api-key",
            "client_secret": "secret-client",
            "access_token": "secret-access",
            "authorization": "secret-auth",
        }
    )

    rendered = str(diagnostic)
    assert "secret-api-key" not in rendered
    assert "secret-client" not in rendered
    assert "secret-access" not in rendered
    assert "secret-auth" not in rendered
