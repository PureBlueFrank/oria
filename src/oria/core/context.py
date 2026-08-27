"""Process-scoped services and immutable per-execution Context."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import TypeVar

from oria.config.models import ResolvedRuntimeConfig
from oria.core.protocols import (
    CacheStore,
    Embedder,
    Guardrail,
    IngressAdapter,
    LLMProvider,
    Memory,
    Node,
    Notifier,
    ObjectStore,
    PolicyEngine,
    Retriever,
    Tool,
)
from oria.core.registry import ServiceRegistry
from oria.core.types import Principal
from oria.domain.services import DomainServiceRegistry

T = TypeVar("T")


class LifecycleSealedError(RuntimeError):
    """Raised when process teardown is modified after runtime startup."""


class LifecycleClosedError(RuntimeError):
    """Raised when a closed process teardown stack is reused."""


class RuntimeSealedError(RuntimeError):
    """Raised when process-scoped services are changed after construction."""


class SealedAsyncExitStack:
    """AsyncExitStack wrapper whose registration phase can be permanently sealed."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sealed = False
        self._closed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def closed(self) -> bool:
        return self._closed

    async def enter_async_context(self, manager: AbstractAsyncContextManager[T]) -> T:
        if self._closed:
            raise LifecycleClosedError("process teardown stack is closed")
        if self._sealed:
            raise LifecycleSealedError("process teardown registration is sealed")
        return await self._stack.enter_async_context(manager)

    def seal(self) -> None:
        self._sealed = True

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stack.aclose()


class RuntimeServices:
    """Resources shared for one process; never stores actor, tenant or run metadata."""

    __slots__ = (
        "_exit_stack",
        "_sealed",
        "agents",
        "cache",
        "config",
        "domain",
        "embedder",
        "guardrails",
        "ingress",
        "llm",
        "memory",
        "nodes",
        "notifier",
        "objects",
        "policy",
        "retriever",
        "tools",
    )

    def __init__(
        self,
        *,
        config: ResolvedRuntimeConfig,
        policy: PolicyEngine,
        domain: DomainServiceRegistry,
        tools: ServiceRegistry[Tool],
        guardrails: ServiceRegistry[Guardrail],
        nodes: ServiceRegistry[Node],
        agents: ServiceRegistry[object],
        ingress: ServiceRegistry[IngressAdapter],
        notifier: ServiceRegistry[Notifier],
        exit_stack: SealedAsyncExitStack,
        llm: LLMProvider | None = None,
        retriever: Retriever | None = None,
        embedder: Embedder | None = None,
        memory: Memory | None = None,
        cache: CacheStore | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        object.__setattr__(self, "_sealed", False)
        self.config = config
        self.llm = llm
        self.tools = tools
        self.retriever = retriever
        self.embedder = embedder
        self.memory = memory
        self.guardrails = guardrails
        self.nodes = nodes
        self.agents = agents
        self.policy = policy
        self.domain = domain
        self.cache = cache
        self.objects = objects
        self.ingress = ingress
        self.notifier = notifier
        self._exit_stack = exit_stack
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise RuntimeSealedError(f"runtime services are sealed; cannot assign {name!r}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            raise RuntimeSealedError(f"runtime services are sealed; cannot delete {name!r}")
        object.__delattr__(self, name)

    @property
    def ready(self) -> bool:
        return self._exit_stack.sealed and not self._exit_stack.closed

    def new_context(
        self,
        *,
        actor: Principal,
        executor: Principal,
        session_id: str,
        thread_id: str,
        run_id: str,
        job_id: str | None = None,
    ) -> Context:
        if not self.ready:
            raise RuntimeError("runtime is not ready")
        if actor.tenant_id != executor.tenant_id:
            raise ValueError("actor and executor must belong to the same tenant")
        return Context(
            runtime=self,
            actor=actor,
            executor=executor,
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            job_id=job_id,
        )

    async def aclose(self) -> None:
        await self._exit_stack.aclose()

    async def __aenter__(self) -> RuntimeServices:
        if not self.ready:
            raise RuntimeError("runtime is not ready")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


@dataclass(frozen=True, slots=True)
class Context:
    """Immutable metadata and read-only service forwarding for one execution."""

    runtime: RuntimeServices
    actor: Principal
    executor: Principal
    session_id: str
    thread_id: str
    run_id: str
    job_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.session_id, self.thread_id, self.run_id)):
            raise ValueError("session_id, thread_id and run_id must be non-empty")
        if self.actor.tenant_id != self.executor.tenant_id:
            raise ValueError("actor and executor must belong to the same tenant")

    @property
    def tenant_id(self) -> str:
        return self.actor.tenant_id

    @property
    def config(self) -> ResolvedRuntimeConfig:
        return self.runtime.config

    @property
    def llm(self) -> LLMProvider | None:
        return self.runtime.llm

    @property
    def tools(self) -> ServiceRegistry[Tool]:
        return self.runtime.tools

    @property
    def retriever(self) -> Retriever | None:
        return self.runtime.retriever

    @property
    def embedder(self) -> Embedder | None:
        return self.runtime.embedder

    @property
    def memory(self) -> Memory | None:
        return self.runtime.memory

    @property
    def guardrails(self) -> ServiceRegistry[Guardrail]:
        return self.runtime.guardrails

    @property
    def nodes(self) -> ServiceRegistry[Node]:
        return self.runtime.nodes

    @property
    def agents(self) -> ServiceRegistry[object]:
        return self.runtime.agents

    @property
    def policy(self) -> PolicyEngine:
        return self.runtime.policy

    @property
    def domain(self) -> DomainServiceRegistry:
        return self.runtime.domain

    @property
    def cache(self) -> CacheStore | None:
        return self.runtime.cache

    @property
    def objects(self) -> ObjectStore | None:
        return self.runtime.objects

    @property
    def ingress(self) -> ServiceRegistry[IngressAdapter]:
        return self.runtime.ingress

    @property
    def notifier(self) -> ServiceRegistry[Notifier]:
        return self.runtime.notifier
