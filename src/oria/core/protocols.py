"""Async Protocol seams for Oria runtime capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from oria.core.types import (
    AuthorizationRequest,
    ChatOptions,
    ChatResult,
    Doc,
    GuardrailResult,
    InboundMessage,
    InboundRequest,
    IngressContext,
    MemoryItem,
    Message,
    NodeResult,
    PolicyDecision,
    ProviderCapabilities,
    QueryFilters,
    SendResult,
    ServiceHealth,
    StreamEvent,
    ToolPolicy,
    ToolResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from oria.core.context import Context


class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult: ...

    def chat_stream(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def capabilities(self, ctx: Context) -> ProviderCapabilities: ...


class PolicyEngine(Protocol):
    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision: ...


class Tool(Protocol):
    name: str
    schema_version: int
    description: str
    json_schema: dict[str, Any]
    result_schema: dict[str, Any]
    policy: ToolPolicy

    def validate_params(self, params: dict[str, Any]) -> None: ...

    async def run(self, params: dict[str, Any], ctx: Context) -> ToolResult: ...


class Retriever(Protocol):
    async def retrieve(
        self,
        query: str,
        ctx: Context,
        k: int = 5,
        query_filters: QueryFilters | None = None,
    ) -> list[Doc]: ...


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str], ctx: Context) -> list[list[float]]: ...


class Memory(Protocol):
    async def load(self, ctx: Context) -> list[Message]: ...

    async def append(self, msg: Message, ctx: Context) -> None: ...

    async def search(self, query: str, ctx: Context, k: int = 5) -> list[MemoryItem]: ...

    async def compress(self, ctx: Context) -> None: ...


class Node(Protocol):
    async def execute(self, state: dict[str, Any], ctx: Context) -> NodeResult: ...


class Guardrail(Protocol):
    phase: Literal["input", "output", "tool"]

    async def check(self, content: Any, ctx: Context) -> GuardrailResult: ...


class IngressAdapter(Protocol):
    name: str

    async def verify_and_normalize(
        self, request: InboundRequest, ingress_ctx: IngressContext
    ) -> InboundMessage: ...


class Notifier(Protocol):
    name: str

    async def send_message(self, target: str, text: str, ctx: Context) -> SendResult: ...

    async def send_file(self, target: str, file_path: str, ctx: Context) -> SendResult: ...


class CacheStore(Protocol):
    async def get(self, key: str, ctx: Context) -> bytes | None: ...

    async def set(self, key: str, val: bytes, ctx: Context, *, ttl: int | None = None) -> None: ...

    async def delete(self, key: str, ctx: Context) -> None: ...


class ObjectStore(Protocol):
    async def put(self, key: str, path: str, ctx: Context) -> str: ...

    async def get(self, key: str, dest: str, ctx: Context) -> str: ...

    def put_bytes(self, key: str, data: bytes, ctx: Context) -> str: ...


class DomainService(Protocol):
    """Base seam implemented by typed domain services introduced in later tasks."""

    service_name: str

    async def health(self, ctx: Context) -> ServiceHealth: ...


class LocalResource(Protocol):
    """A process-scoped local resource with explicit asynchronous teardown."""

    path: Path

    async def aclose(self) -> None: ...
