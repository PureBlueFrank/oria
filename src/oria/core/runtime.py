"""The single asynchronous factory for process-scoped RuntimeServices."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from oria.config.models import ResolvedRuntimeConfig
from oria.config.resolve import resolve_runtime_config
from oria.core.context import RuntimeServices, SealedAsyncExitStack
from oria.core.protocols import Guardrail, IngressAdapter, Node, Notifier, Tool
from oria.core.registry import ServiceRegistry
from oria.domain.services import DomainServiceRegistry
from oria.ingress.local import LocalCLIIngressAdapter
from oria.permission.local import LocalPolicyEngine

RuntimeResourceFactory = Callable[[], AbstractAsyncContextManager[object]]


async def build_runtime(
    config: ResolvedRuntimeConfig | None = None,
    *,
    resource_factories: Sequence[RuntimeResourceFactory] = (),
) -> RuntimeServices:
    """Build and seal one runtime, unwinding every startup resource on failure."""
    resolved = resolve_runtime_config() if config is None else config
    exit_stack = SealedAsyncExitStack()
    try:
        for factory in resource_factories:
            await exit_stack.enter_async_context(factory())

        tools: ServiceRegistry[Tool] = ServiceRegistry()
        guardrails: ServiceRegistry[Guardrail] = ServiceRegistry()
        nodes: ServiceRegistry[Node] = ServiceRegistry()
        agents: ServiceRegistry[object] = ServiceRegistry()
        ingress: ServiceRegistry[IngressAdapter] = ServiceRegistry()
        notifier: ServiceRegistry[Notifier] = ServiceRegistry()
        ingress.register("cli", LocalCLIIngressAdapter())

        for registry in (tools, guardrails, nodes, agents, ingress, notifier):
            registry.seal()

        runtime = RuntimeServices(
            config=resolved,
            policy=LocalPolicyEngine(),
            domain=DomainServiceRegistry(),
            tools=tools,
            guardrails=guardrails,
            nodes=nodes,
            agents=agents,
            ingress=ingress,
            notifier=notifier,
            exit_stack=exit_stack,
        )
        exit_stack.seal()
        return runtime
    except BaseException:
        await exit_stack.aclose()
        raise
