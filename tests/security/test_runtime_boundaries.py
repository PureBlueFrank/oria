"""Security tests for policy boundaries, redaction and identity impersonation (V0.1-T02)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from click import Group
from pydantic import SecretStr, ValidationError
from typer.main import get_command
from typer.testing import CliRunner

from oria.cli import app
from oria.config import resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import Context, RuntimeServices
from oria.core.runtime import build_runtime
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    ChatResult,
    InboundMessage,
    InboundRequest,
    IngressContext,
    Principal,
    ReasoningDelta,
    ResourceRef,
    SecretValue,
    TextBlock,
    Usage,
)
from oria.ingress.local import IngressVerificationError, LocalCLIIngressAdapter
from oria.permission.local import (
    LOCAL_CLI_SUBJECT_ID,
    LOCAL_POLICY_VERSION,
    LOCAL_TENANT_ID,
    LOCAL_USER_SUBJECT_ID,
    LocalPolicyEngine,
    local_cli_executor,
    local_operator,
)

pytestmark = pytest.mark.security


def _resolve_config(tmp_path: Path) -> ResolvedRuntimeConfig:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    return resolve_runtime_config(
        config_path=config_path,
        environ={},
        data_dir=tmp_path / "data",
    )


def _principal(
    subject_id: str,
    tenant_id: str,
    *,
    kind: str = "human",
    roles: tuple[str, ...] = ("operator",),
    authn_method: str = "test-identity",
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        kind=kind,
        roles=roles,
        authn_method=authn_method,
    )


def _auth_request(
    actor: Principal,
    executor: Principal,
    *,
    action: str = "config:read",
    resource_tenant: str | None = None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        actor=actor,
        executor=executor,
        action=action,
        resource=ResourceRef(
            resource_type="config",
            resource_id="runtime",
            tenant_id=LOCAL_TENANT_ID if resource_tenant is None else resource_tenant,
        ),
        context=AuthorizationContext(correlation_id="corr-boundary"),
    )


def _context_for(
    runtime: RuntimeServices, actor: Principal, executor: Principal, suffix: str
) -> Context:
    return runtime.new_context(
        actor=actor,
        executor=executor,
        session_id=f"session-{suffix}",
        thread_id=f"thread-{suffix}",
        run_id=f"run-{suffix}",
    )


@pytest.mark.asyncio
async def test_local_policy_rejects_cross_tenant_and_untrusted_requests(tmp_path: Path) -> None:
    """V01-CTX-01b: local policy denies spoofed, cross-tenant and disallowed requests."""
    config = _resolve_config(tmp_path)
    runtime = await build_runtime(config)
    try:
        assert isinstance(runtime.policy, LocalPolicyEngine)

        actor = local_operator()
        executor = local_cli_executor()
        ctx = _context_for(runtime, actor, executor, "policy")

        allowed = await runtime.policy.authorize(_auth_request(actor, executor), ctx)
        assert allowed.allow is True
        assert allowed.policy_version == LOCAL_POLICY_VERSION == "local-v1"
        assert allowed.constraints == {"tenant_id": LOCAL_TENANT_ID}
        assert allowed.reason == "allowed by trusted local profile"

        attacker = _principal("attacker", LOCAL_TENANT_ID, roles=("admin",))
        mismatched = await runtime.policy.authorize(_auth_request(attacker, executor), ctx)
        assert mismatched.allow is False
        assert mismatched.reason == "authorization principals do not match the trusted context"
        assert mismatched.constraints == {}
        assert mismatched.policy_version == LOCAL_POLICY_VERSION

        cross_tenant = await runtime.policy.authorize(
            _auth_request(actor, executor, resource_tenant="tenant-evil"), ctx
        )
        assert cross_tenant.allow is False
        assert cross_tenant.reason == "cross-tenant access is denied"

        unknown_action = await runtime.policy.authorize(
            _auth_request(actor, executor, action="campaign:delete"), ctx
        )
        assert unknown_action.allow is False
        assert unknown_action.reason == "action is not allowed by the local read policy"
    finally:
        await runtime.aclose()


def test_outputs_and_serializations_do_not_leak_secrets(tmp_path: Path) -> None:
    """V01-LOG-01: repr/JSON outputs never leak raw bodies, reasoning or secrets."""
    raw_body_secret = "raw-body-payload-6f2a91c4e8d0"
    request = InboundRequest(
        headers={"content-type": "text/plain"},
        raw_body=raw_body_secret.encode("utf-8"),
        received_at=datetime.now(UTC),
        request_id="req-log-01",
    )
    assert request.raw_body == raw_body_secret.encode("utf-8")

    assert "raw_body" not in request.model_dump()
    request_json = request.model_dump_json()
    assert "raw_body" not in json.loads(request_json)
    assert raw_body_secret not in request_json
    assert raw_body_secret not in repr(request)

    reasoning_secret = "internal-chain-of-thought-2b7d"
    reasoning = ReasoningDelta(
        sequence=1,
        provider="mock",
        model="mock-demo",
        text=reasoning_secret,
    )
    assert reasoning.text == reasoning_secret
    assert reasoning_secret not in repr(reasoning)
    assert "reasoning_delta" in repr(reasoning)
    assert reasoning_secret not in reasoning.model_dump_json()

    api_secret = "sk-live-4e8f1a306547bd9d2b7c"
    secret = SecretValue(value=SecretStr(api_secret))
    assert secret.value.get_secret_value() == api_secret
    assert api_secret not in repr(secret)
    assert api_secret not in str(secret)

    provider_payload_secret = "provider-internal-payload-9c1e"
    chat_result = ChatResult(
        content=[TextBlock(text="campaign summary")],
        tool_calls=[],
        usage=Usage(input_tokens=3, output_tokens=5),
        raw_response={"internal": provider_payload_secret},
    )
    assert chat_result.raw_response == {"internal": provider_payload_secret}
    assert provider_payload_secret not in repr(chat_result)
    assert provider_payload_secret not in chat_result.model_dump_json()

    provider_key = "sk-live-9d2b7c4e8f1a306547bd"
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    cli_result = CliRunner().invoke(
        app,
        [
            "config",
            "doctor",
            "--output",
            "json",
            "--config",
            str(config_path),
            "--llm-profile",
            "deepseek",
            "--data-dir",
            str(tmp_path / "data"),
        ],
        env={"DEEPSEEK_API_KEY": provider_key},
    )
    assert cli_result.exit_code == 0
    assert provider_key not in cli_result.stdout
    payload = json.loads(cli_result.stdout)
    assert payload["ok"] is True
    assert payload["config"]["llm"]["credential_configured"] is True


@pytest.mark.asyncio
async def test_cli_and_local_identities_cannot_be_impersonated(tmp_path: Path) -> None:
    """tenant/roles 不可冒充: the CLI offers no identity flags and forged principals are denied."""
    root_command = cast(Group, get_command(app))
    config_command = cast(Group, root_command.commands["config"])
    doctor_command = config_command.commands["doctor"]
    option_names = {
        option for parameter in doctor_command.params for option in getattr(parameter, "opts", ())
    }
    scenario_groups = [
        cast(Group, root_command.commands[name]) for name in ("workflow", "approval", "mock")
    ]
    scenario_commands = [
        command for group in scenario_groups for command in group.commands.values()
    ]
    option_names.update(
        option
        for command in scenario_commands
        for parameter in command.params
        for option in getattr(parameter, "opts", ())
    )
    for forbidden_flag in ("--tenant", "--roles", "--subject", "--actor"):
        assert forbidden_flag not in option_names

    operator = local_operator()
    executor = local_cli_executor()
    assert operator.tenant_id == LOCAL_TENANT_ID == "local-community"
    assert operator.subject_id == LOCAL_USER_SUBJECT_ID == "local-operator"
    assert operator.kind == "human"
    assert operator.roles == ("operator",)
    assert operator.authn_method == "trusted-local-profile"
    assert executor.tenant_id == LOCAL_TENANT_ID
    assert executor.subject_id == LOCAL_CLI_SUBJECT_ID == "oria-cli"
    assert executor.kind == "service"
    assert executor.roles == ("runtime",)
    assert executor.authn_method == "trusted-local-profile"
    assert local_operator() == operator
    assert local_cli_executor() == executor

    forged_actor = _principal(
        "mallory",
        LOCAL_TENANT_ID,
        roles=("admin", "operator"),
        authn_method="asserted-by-attacker",
    )
    forged_executor = _principal(
        "mallory-runtime",
        LOCAL_TENANT_ID,
        kind="service",
        roles=("runtime",),
        authn_method="asserted-by-attacker",
    )
    config = _resolve_config(tmp_path)
    runtime = await build_runtime(config)
    try:
        forged_ctx = _context_for(runtime, forged_actor, forged_executor, "spoof")
        decision = await runtime.policy.authorize(
            _auth_request(forged_actor, forged_executor), forged_ctx
        )
        assert decision.allow is False
        assert decision.reason == "principal is not the trusted community identity"
        assert decision.constraints == {}
    finally:
        await runtime.aclose()

    with pytest.raises(ValidationError):
        InboundMessage(
            source="cli",
            source_message_id="msg-spoof",
            mapped_tenant_id=LOCAL_TENANT_ID,
            mapped_subject_id=LOCAL_USER_SUBJECT_ID,
            sender_ref=LOCAL_CLI_SUBJECT_ID,
            text="spoofed body",
            received_at=datetime.now(UTC),
            verified=False,
            dedupe_key="cli:msg-spoof:spoofed",
        )


@pytest.mark.asyncio
async def test_local_cli_ingress_enforces_fixed_identity_mapping() -> None:
    """入站身份固定映射: CLI ingress identity comes from constants, not request input."""
    adapter = LocalCLIIngressAdapter()
    now = datetime.now(UTC)
    body = "summarize today's campaigns"
    request = InboundRequest(
        headers={"x-spoofed-tenant": "tenant-evil", "x-spoofed-roles": "admin"},
        raw_body=body.encode("utf-8"),
        received_at=now,
        request_id="req-ing-01",
    )
    trusted_ctx = IngressContext(
        executor=local_cli_executor(),
        request_id="req-ing-01",
        correlation_id="corr-ing-01",
    )

    message = await adapter.verify_and_normalize(request, trusted_ctx)
    assert message.verified is True
    assert message.source == "cli"
    assert message.mapped_tenant_id == LOCAL_TENANT_ID
    assert message.mapped_subject_id == LOCAL_USER_SUBJECT_ID
    assert message.sender_ref == LOCAL_CLI_SUBJECT_ID
    assert message.text == body
    assert message.dedupe_key.startswith("cli:req-ing-01:")

    with pytest.raises(IngressVerificationError):
        await adapter.verify_and_normalize(
            request,
            IngressContext(
                executor=local_cli_executor(),
                request_id="req-mismatch",
                correlation_id="corr-ing-01",
            ),
        )

    with pytest.raises(IngressVerificationError):
        await adapter.verify_and_normalize(
            request,
            IngressContext(
                executor=_principal(
                    "fake-cli", LOCAL_TENANT_ID, kind="service", roles=("runtime",)
                ),
                request_id="req-ing-01",
                correlation_id="corr-ing-01",
            ),
        )

    invalid_utf8 = InboundRequest(
        headers={},
        raw_body=b"\xff\xfe not utf-8",
        received_at=now,
        request_id="req-ing-02",
    )
    with pytest.raises(IngressVerificationError):
        await adapter.verify_and_normalize(
            invalid_utf8,
            IngressContext(
                executor=local_cli_executor(),
                request_id="req-ing-02",
                correlation_id="corr-ing-01",
            ),
        )

    with pytest.raises(ValidationError):
        InboundRequest(
            headers={},
            raw_body=b"x",
            received_at=datetime(2026, 1, 1),
            request_id="req-ing-naive",
        )
