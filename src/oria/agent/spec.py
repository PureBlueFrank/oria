"""Static configuration for the single bounded research-agent loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from oria.core.types import JsonValue, ResponseSchema, ToolCall, ToolSpec, ValueModel

ResearchStateView: TypeAlias = Mapping[str, Any]
ToolSpecAdapter: TypeAlias = Callable[[tuple[ToolSpec, ...], ResearchStateView], list[ToolSpec]]
ToolCallValidator: TypeAlias = Callable[[ToolCall, ResearchStateView], None]
Finalizer: TypeAlias = Callable[[dict[str, JsonValue], ResearchStateView], ValueModel]


def unchanged_tool_specs(specs: tuple[ToolSpec, ...], state: ResearchStateView) -> list[ToolSpec]:
    del state
    return list(specs)


def accept_tool_call(call: ToolCall, state: ResearchStateView) -> None:
    del call, state


@dataclass(frozen=True, slots=True)
class ResearchSpec:
    """Scenario-owned seams around the shared model/tool/validate loop."""

    prompt_name: str
    prompt_version: int
    tool_names: tuple[str, ...]
    response_schema: ResponseSchema
    output_field: str
    validated_event_type: str
    finalize: Finalizer
    result_state_fields: tuple[tuple[str, str], ...] = ()
    adapt_tool_specs: ToolSpecAdapter = unchanged_tool_specs
    validate_tool_call: ToolCallValidator = accept_tool_call

    def __post_init__(self) -> None:
        if not self.tool_names or len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("research tool names must be non-empty and unique")
        if self.prompt_version < 1:
            raise ValueError("research prompt version must be positive")
        mapped_tools = tuple(tool_name for tool_name, _ in self.result_state_fields)
        if len(set(mapped_tools)) != len(mapped_tools):
            raise ValueError("research result tool mappings must be unique")
        if not set(mapped_tools).issubset(self.tool_names):
            raise ValueError("research result mappings must reference allowed tools")
