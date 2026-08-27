"""The single asynchronous factory for process-scoped RuntimeServices."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from oria.config.models import ResolvedRuntimeConfig
from oria.config.resolve import resolve_runtime_config
from oria.core.context import RuntimeServices, SealedAsyncExitStack
from oria.core.protocols import Guardrail, IngressAdapter, Node, Notifier, Tool
from oria.core.registry import ServiceRegistry
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.services import (
    DefaultMerchantService,
    DomainServiceRegistry,
    PackageCampaignRuleService,
)
from oria.ingress.local import LocalCLIIngressAdapter
from oria.permission.local import LocalPolicyEngine
from oria.resources.loader import load_demo_data
from oria.storage.database import DatabaseResources
from oria.storage.repositories import SQLiteMerchantRepository

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
        database_resources: DatabaseResources | None = None
        for factory in resource_factories:
            resource = await exit_stack.enter_async_context(factory())
            if isinstance(resource, DatabaseResources):
                if database_resources is not None:
                    raise ValueError("only one database resource may be injected")
                database_resources = resource
        if database_resources is None:
            database_resources = await exit_stack.enter_async_context(DatabaseResources(resolved))

        bundle = load_demo_data()
        campaign_rules = PackageCampaignRuleService(bundle.rules)
        merchant_repository = SQLiteMerchantRepository(database_resources.business_sessions)
        merchant_service = DefaultMerchantService(
            merchant_repository,
            EligibilityPolicy(),
            campaign_rules,
        )
        domain = DomainServiceRegistry(
            campaign_rules=campaign_rules,
            merchants=merchant_service,
        )

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
            domain=domain,
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
