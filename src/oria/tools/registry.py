"""Startup-sealed ToolRegistry with allowlist, authorization, and schema enforcement."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from jsonschema import FormatChecker, validators

from oria.core.context import Context
from oria.core.protocols import Tool
from oria.core.registry import RegistrySealedError
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    JsonValue,
    ResourceRef,
    ResponseSchema,
    ToolResult,
    ToolSpec,
)


class ToolRegistry:
    """Expose only configured tools and enforce their declared contracts at execution."""

    def __init__(self, *, allowlist: frozenset[str]) -> None:
        if not allowlist:
            raise ValueError("tool allowlist must be non-empty")
        self._allowlist = allowlist
        self._items: dict[str, Tool] | Mapping[str, Tool] = {}
        self._specs: dict[str, ToolSpec] | Mapping[str, ToolSpec] = {}
        self._result_schemas: (
            dict[str, dict[str, JsonValue]] | Mapping[str, dict[str, JsonValue]]
        ) = {}
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def allowlist(self) -> frozenset[str]:
        return self._allowlist

    def register(self, name_or_tool: str | Tool, service: Tool | None = None) -> None:
        if self._sealed or not isinstance(self._items, dict):
            raise RegistrySealedError("runtime registry registration is sealed")
        if isinstance(name_or_tool, str):
            if service is None or service.name != name_or_tool:
                raise ValueError("tool registration name must match the tool contract")
            tool = service
        else:
            if service is not None:
                raise ValueError("tool service must be omitted when registering by contract")
            tool = name_or_tool
        if tool.name not in self._allowlist:
            raise ValueError("tool is not present in the configured allowlist")
        if tool.name in self._items:
            raise ValueError(f"duplicate tool registration: {tool.name!r}")
        spec = ToolSpec(
            name=tool.name,
            schema_version=tool.schema_version,
            description=tool.description,
            json_schema=tool.json_schema,
        )
        result_schema = ResponseSchema(
            name=f"{tool.name}_result",
            json_schema=tool.result_schema,
        ).json_schema
        self._items[tool.name] = tool
        if not isinstance(self._specs, dict) or not isinstance(self._result_schemas, dict):
            raise RegistrySealedError("runtime registry registration is sealed")
        self._specs[tool.name] = spec
        self._result_schemas[tool.name] = result_schema

    def seal(self) -> None:
        if set(self._items) != set(self._allowlist):
            raise ValueError("every allowlisted tool must be registered before sealing")
        if not self._sealed:
            self._items = MappingProxyType(dict(self._items))
            self._specs = MappingProxyType(dict(self._specs))
            self._result_schemas = MappingProxyType(dict(self._result_schemas))
            self._sealed = True

    def get(self, name: str) -> Tool:
        if name not in self._allowlist:
            raise LookupError("tool is not allowlisted")
        try:
            return self._items[name]
        except KeyError as exc:
            raise LookupError("tool is unavailable") from exc

    def specs(self, names: Sequence[str] | None = None) -> tuple[ToolSpec, ...]:
        selected = tuple(self._items) if names is None else tuple(names)
        if len(set(selected)) != len(selected):
            raise ValueError("tool selection contains duplicates")
        for name in selected:
            self.get(name)
        return tuple(self._specs[name] for name in selected)

    async def preflight(self, name: str, params: dict[str, Any], ctx: Context) -> None:
        """Validate one call without invoking the tool implementation."""

        tool = self.get(name)
        input_schema = self._specs[name].json_schema
        validators.validator_for(input_schema)(
            input_schema, format_checker=FormatChecker()
        ).validate(params)
        tool.validate_params(params)
        decision = await ctx.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action=tool.policy.required_action,
                resource=ResourceRef(
                    resource_type=tool.policy.resource_type,
                    resource_id=tool.name,
                    tenant_id=ctx.tenant_id,
                ),
                context=AuthorizationContext(correlation_id=ctx.run_id),
            ),
            ctx,
        )
        if not decision.allow or decision.constraints.get("tenant_id") != ctx.tenant_id:
            raise PermissionError("tool execution is not authorized")

    async def execute(self, name: str, params: dict[str, Any], ctx: Context) -> ToolResult:
        await self.preflight(name, params, ctx)
        tool = self.get(name)
        result_schema = self._result_schemas[name]
        retry = tool.policy.retry_policy
        for attempt in range(1, retry.max_attempts + 1):
            async with asyncio.timeout(tool.policy.timeout_seconds):
                result = await tool.run(params, ctx)
            if result.ok:
                validators.validator_for(result_schema)(
                    result_schema, format_checker=FormatChecker()
                ).validate(result.data)
                return result
            if (
                tool.policy.side_effect
                or result.error is None
                or not result.error.retryable
                or attempt == retry.max_attempts
            ):
                return result
            delay = min(
                retry.initial_backoff_seconds * (2 ** (attempt - 1)),
                retry.max_backoff_seconds,
            )
            if delay:
                await asyncio.sleep(delay)
        raise AssertionError("tool retry loop did not return")

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)
