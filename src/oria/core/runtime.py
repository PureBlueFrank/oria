"""The single asynchronous factory for process-scoped RuntimeServices."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

import httpx

from oria.agent.graph import build_research_graph
from oria.config.models import ResolvedRuntimeConfig
from oria.config.resolve import resolve_runtime_config
from oria.core.context import RuntimeServices, SealedAsyncExitStack
from oria.core.protocols import (
    Embedder,
    Guardrail,
    IngressAdapter,
    LLMProvider,
    Node,
    Notifier,
)
from oria.core.registry import ServiceRegistry
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.services import (
    DefaultMerchantService,
    DomainServiceRegistry,
    PackageCampaignRuleService,
)
from oria.ingress.local import LocalCLIIngressAdapter
from oria.orchestrator.checkpoint import open_tenant_sqlite_saver
from oria.permission.local import LocalPolicyEngine
from oria.providers.demo import DemoMockLLMProvider
from oria.providers.embeddings import BGEEmbedder, FixtureEmbedder
from oria.providers.openai_compat import OpenAICompatProvider
from oria.rag.catalog import SQLiteKnowledgeCatalog
from oria.rag.index import ChromaIndex
from oria.rag.object_store import LocalObjectStore
from oria.rag.service import AuthorizedChromaRetriever, LocalKnowledgeService
from oria.rag.snapshots import LocalRuleSnapshotStore
from oria.resources.loader import load_demo_data
from oria.storage.database import DatabaseResources
from oria.storage.repositories import SQLiteMerchantRepository
from oria.tools.builtin import QueryMerchantsTool, SearchCampaignRulesTool
from oria.tools.registry import ToolRegistry

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
        llm: LLMProvider
        if resolved.llm.provider == "mock":
            llm = DemoMockLLMProvider()
        elif resolved.llm.provider == "deepseek":
            if resolved.llm.base_url is None:
                raise ValueError("DeepSeek profile requires a base_url")
            http_client = await exit_stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=resolved.llm.base_url,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                )
            )
            llm = OpenAICompatProvider(resolved.llm, http_client)
        else:
            raise ValueError(f"unsupported LLM provider: {resolved.llm.provider}")

        embedder: Embedder
        if resolved.embedding.provider == "fixture":
            embedder = FixtureEmbedder()
        elif resolved.embedding.provider == "sentence_transformers":
            if resolved.embedding.model is None:
                raise ValueError("sentence-transformers profile requires a model")
            embedder = await asyncio.to_thread(
                BGEEmbedder,
                model=resolved.embedding.model,
                revision=resolved.embedding.revision,
                trust_remote_code=resolved.embedding.trust_remote_code,
            )
        else:
            raise ValueError(f"unsupported embedding provider: {resolved.embedding.provider}")

        database_resources: DatabaseResources | None = None
        for factory in resource_factories:
            resource = await exit_stack.enter_async_context(factory())
            if isinstance(resource, DatabaseResources):
                if database_resources is not None:
                    raise ValueError("only one database resource may be injected")
                database_resources = resource
        if database_resources is None:
            database_resources = await exit_stack.enter_async_context(DatabaseResources(resolved))
        checkpointer = await exit_stack.enter_async_context(
            open_tenant_sqlite_saver(resolved.data_paths.platform_db)
        )

        if resolved.storage.object != "local" or resolved.storage.vector != "chroma":
            raise ValueError("selected knowledge storage implementation is unavailable")
        objects = await exit_stack.enter_async_context(
            LocalObjectStore(resolved.data_paths.objects, resolved.data_paths.root)
        )
        embedding_projection = (
            "sha256:" + hashlib.sha256(resolved.embedding.model_dump_json().encode()).hexdigest()
        )
        index = await exit_stack.enter_async_context(
            ChromaIndex(
                resolved.data_paths.chroma,
                projection_id=embedding_projection,
                embedding_dimension=embedder.dim,
            )
        )
        catalog = SQLiteKnowledgeCatalog(database_resources.platform_sessions)
        knowledge = LocalKnowledgeService(
            catalog=catalog,
            objects=objects,
            index=index,
            embedder=embedder,
            embedding_profile=embedding_projection,
        )
        retriever = AuthorizedChromaRetriever(
            catalog=catalog,
            index=index,
            embedder=embedder,
            knowledge=knowledge,
        )
        rule_snapshots = LocalRuleSnapshotStore(catalog, knowledge)

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

        tools = ToolRegistry(allowlist=frozenset({"search_campaign_rules", "query_merchants"}))
        tools.register(SearchCampaignRulesTool(retriever, rule_snapshots))
        tools.register(QueryMerchantsTool(rule_snapshots, merchant_service))
        guardrails: ServiceRegistry[Guardrail] = ServiceRegistry()
        nodes: ServiceRegistry[Node] = ServiceRegistry()
        agents: ServiceRegistry[object] = ServiceRegistry()
        ingress: ServiceRegistry[IngressAdapter] = ServiceRegistry()
        notifier: ServiceRegistry[Notifier] = ServiceRegistry()
        ingress.register("cli", LocalCLIIngressAdapter())
        agents.register("research_agent", build_research_graph(checkpointer=checkpointer))

        tools.seal()
        for registry in (guardrails, nodes, agents, ingress, notifier):
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
            llm=llm,
            embedder=embedder,
            retriever=retriever,
            objects=objects,
            knowledge=knowledge,
            rule_snapshots=rule_snapshots,
        )
        exit_stack.seal()
        return runtime
    except BaseException:
        await exit_stack.aclose()
        raise
