"""Least-privilege boundaries for supervisor-owned subagents."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from oria.agent import authorized_subagent_tools, campaign_research_spec
from oria.core.context import Context
from oria.core.protocols import Guardrail
from oria.core.registry import ServiceRegistry
from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.guardrails import ToolAuthorizationGuardrail
from oria.permission.local import LocalPolicyEngine, local_cli_executor, local_operator
from oria.tools.registry import ToolRegistry

pytestmark = pytest.mark.security


class _Tool:
    schema_version = 1
    description = "supervisor security fixture"

    def __init__(self, name: str, action: str) -> None:
        self.name = name
        self.runs = 0
        self.json_schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        self.result_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"ran": {"type": "boolean"}},
            "required": ["ran"],
            "additionalProperties": False,
        }
        self.policy = ToolPolicy(
            risk_level="low",
            side_effect=False,
            timeout_seconds=1,
            retry_policy=RetryPolicy(max_attempts=1),
            required_action=action,
            resource_type="supervisor_fixture",
            approval_mode="none",
        )

    def validate_params(self, params: dict[str, Any]) -> None:
        assert params == {}

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult:
        del params, ctx
        self.runs += 1
        return ToolResult(
            ok=True,
            data={"ran": True},
            execution_id=f"run-{self.name}",
            trust_level="test",
            provenance="test",
            data_classification="internal",
        )


def _context(registry: ToolRegistry) -> Context:
    actor = local_operator()
    executor = local_cli_executor()
    policy = LocalPolicyEngine(trusted_actors=(actor,), trusted_executors=(executor,))
    guardrails: ServiceRegistry[Guardrail] = ServiceRegistry()
    guardrails.register("tool.authorization", ToolAuthorizationGuardrail())
    guardrails.seal()
    runtime = SimpleNamespace(
        tools=registry,
        policy=policy,
        guardrails=guardrails,
    )
    return cast(
        Context,
        SimpleNamespace(
            runtime=runtime,
            tools=registry,
            policy=policy,
            guardrails=guardrails,
            actor=actor,
            executor=executor,
            tenant_id=actor.tenant_id,
            correlation_id="supervisor-security-correlation",
            run_id="supervisor-security-run",
        ),
    )


@pytest.mark.asyncio
async def test_subagent_visibility_is_allowlist_intersection_and_execution_reauthorizes() -> None:
    tools = (
        _Tool("search_campaign_rules", "rule:read"),
        _Tool("query_merchants", "campaign:draft:write"),
        _Tool("supervisor_admin", "rule:read"),
    )
    registry = ToolRegistry(allowlist=frozenset(tool.name for tool in tools))
    for tool in tools:
        registry.register(tool)
    registry.seal()
    ctx = _context(registry)

    visible = await authorized_subagent_tools(campaign_research_spec(), ctx)

    assert visible == ("search_campaign_rules",)
    assert set(visible) <= set(campaign_research_spec().tool_names)
    assert "supervisor_admin" not in visible
    with pytest.raises(PermissionError, match="not authorized"):
        await registry.execute("query_merchants", {}, ctx)
    assert tools[1].runs == 0
