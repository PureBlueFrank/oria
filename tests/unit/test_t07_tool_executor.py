"""Read-only retry ownership tests for the ToolRegistry executor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

from oria.core.types import (
    PolicyDecision,
    Principal,
    RetryPolicy,
    ToolError,
    ToolPolicy,
    ToolResult,
)
from oria.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


class _AllowPolicy:
    async def authorize(self, request: object, ctx: object) -> PolicyDecision:
        del request, ctx
        return PolicyDecision(
            allow=True,
            constraints={"tenant_id": "tenant"},
            policy_version="test-v1",
            reason="test allow",
        )


class _RetryTool:
    name = "read_test"
    schema_version = 1
    description = "retry test"
    json_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {"done": {"type": "boolean"}},
        "required": ["done"],
        "additionalProperties": False,
    }

    def __init__(self, *, side_effect: bool) -> None:
        self.calls = 0
        self.policy = ToolPolicy(
            risk_level="low",
            side_effect=side_effect,
            timeout_seconds=1,
            retry_policy=RetryPolicy(
                max_attempts=3,
                initial_backoff_seconds=0,
                max_backoff_seconds=0,
            ),
            required_action="rule:read",
            resource_type="test",
            approval_mode="none",
        )

    def validate_params(self, params: dict[str, object]) -> None:
        del params

    async def run(self, params: dict[str, object], ctx: object) -> ToolResult:
        del params, ctx
        self.calls += 1
        if self.calls < 3:
            return ToolResult(
                ok=False,
                error=ToolError(code="transient", safe_message="retry", retryable=True),
                execution_id=f"attempt-{self.calls}",
                trust_level="test",
                provenance="test",
                data_classification="test",
            )
        return ToolResult(
            ok=True,
            data={"done": True},
            execution_id="attempt-3",
            trust_level="test",
            provenance="test",
            data_classification="test",
        )


def _context():
    return SimpleNamespace(
        tenant_id="tenant",
        policy=_AllowPolicy(),
        actor=Principal(
            subject_id="actor",
            tenant_id="tenant",
            kind="human",
            roles=("test",),
            authn_method="test",
        ),
        executor=Principal(
            subject_id="executor",
            tenant_id="tenant",
            kind="service",
            roles=("test",),
            authn_method="test",
        ),
        run_id="run",
    )


@pytest.mark.asyncio
async def test_read_only_retryable_failure_is_retried_only_by_executor() -> None:
    tool = _RetryTool(side_effect=False)
    registry = ToolRegistry(allowlist=frozenset({tool.name}))
    registry.register(tool)
    registry.seal()

    result = await registry.execute(tool.name, {}, _context())

    assert result.ok is True
    assert tool.calls == 3


@pytest.mark.asyncio
async def test_side_effect_tool_is_never_automatically_retried() -> None:
    tool = _RetryTool(side_effect=True)
    registry = ToolRegistry(allowlist=frozenset({tool.name}))
    registry.register(tool)
    registry.seal()

    result = await registry.execute(tool.name, {}, _context())

    assert result.ok is False
    assert tool.calls == 1
