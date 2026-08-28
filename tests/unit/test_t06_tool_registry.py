"""Startup and immutability checks for the dedicated ToolRegistry."""

from __future__ import annotations

from typing import ClassVar

import pytest

from oria.core.registry import RegistrySealedError
from oria.core.types import RetryPolicy, ToolPolicy, ToolResult
from oria.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


class _Tool:
    schema_version = 1
    description = "test tool"
    json_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    result_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    policy = ToolPolicy(
        risk_level="low",
        side_effect=False,
        timeout_seconds=1,
        retry_policy=RetryPolicy(),
        required_action="rule:read",
        resource_type="test",
        approval_mode="none",
    )

    def __init__(self, name: str) -> None:
        self.name = name

    def validate_params(self, params: dict[str, object]) -> None:
        del params

    async def run(self, params: dict[str, object], ctx: object) -> ToolResult:
        del params, ctx
        return ToolResult(
            ok=True,
            data={},
            execution_id="test-execution",
            trust_level="test",
            provenance="test",
            data_classification="test",
        )


def test_tool_registry_requires_a_complete_allowlist_before_sealing() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ToolRegistry(allowlist=frozenset())

    registry = ToolRegistry(allowlist=frozenset({"search_campaign_rules"}))
    with pytest.raises(ValueError, match="every allowlisted"):
        registry.seal()


def test_tool_registry_is_sealed_and_specs_are_immutable() -> None:
    registry = ToolRegistry(allowlist=frozenset({"search_campaign_rules", "query_merchants"}))
    registry.register(_Tool("search_campaign_rules"))
    registry.register(_Tool("query_merchants"))
    registry.seal()

    assert registry.sealed is True
    assert tuple(spec.name for spec in registry.specs()) == (
        "search_campaign_rules",
        "query_merchants",
    )
    with pytest.raises(TypeError, match="immutable"):
        registry.specs(("query_merchants",))[0].json_schema["title"] = "tampered"
    with pytest.raises(RegistrySealedError, match="sealed"):
        registry.register(registry.get("search_campaign_rules"))
    with pytest.raises(ValueError, match="duplicates"):
        registry.specs(("query_merchants", "query_merchants"))
