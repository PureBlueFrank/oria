"""The single asynchronous factory for process-scoped RuntimeServices."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from oria.adapters.assortment import (
    InMemoryAssortmentAdapter,
    InMemoryConsumerPlacementAdapter,
    InMemoryMerchantNotificationAdapter,
)
from oria.adapters.launch import InMemoryCouponBatchAdapter, InMemoryRecruitmentAdapter
from oria.adapters.products import InMemoryProductCatalogAdapter
from oria.agent.graph import build_research_graph
from oria.config.models import ResolvedRuntimeConfig
from oria.config.resolve import resolve_runtime_config
from oria.core.approvals import ApprovalBindingInvalidationConsumer, ApprovalService
from oria.core.context import RuntimeServices, SealedAsyncExitStack
from oria.core.execution_ledger import ExecutionLedger
from oria.core.integration_events import IntegrationEventInboxService
from oria.core.protocols import (
    Embedder,
    Guardrail,
    IngressAdapter,
    LLMProvider,
    Node,
    Notifier,
)
from oria.core.registry import ServiceRegistry
from oria.core.types import Principal
from oria.domain.assortment import AssortmentService, TrustedSelectionEventService
from oria.domain.confirmations import ConfirmationService
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.enrollment import (
    CouponLinkService,
    EnrollmentService,
    InMemoryConfirmationSubjectDirectory,
)
from oria.domain.enrollment_branch import EnrollmentBranchCoordinator
from oria.domain.launch import DefaultCampaignLaunchService
from oria.domain.product_eligibility import ProductEligibilityPolicy, ProductSnapshot
from oria.domain.products import ProductQueryService
from oria.domain.services import (
    DefaultMerchantService,
    DomainServiceRegistry,
    PackageCampaignRuleService,
)
from oria.ingress.local import LocalCLIIngressAdapter
from oria.orchestrator.checkpoint import open_tenant_sqlite_saver
from oria.orchestrator.scenario_a import (
    DefaultScenarioAWorkflowService,
    build_scenario_a_graph,
)
from oria.permission.audit import PlatformAuditService
from oria.permission.local import LocalPolicyEngine
from oria.providers.anthropic import AnthropicProvider
from oria.providers.demo import DemoMockLLMProvider
from oria.providers.embeddings import BGEEmbedder, FixtureEmbedder
from oria.providers.openai_compat import OpenAICompatProvider
from oria.rag.bm25 import BM25Index
from oria.rag.catalog import SQLiteKnowledgeCatalog
from oria.rag.index import ChromaIndex
from oria.rag.object_store import LocalObjectStore
from oria.rag.pipeline import ConfigurableRetriever
from oria.rag.service import (
    AuthorizedBM25Retriever,
    AuthorizedChromaRetriever,
    LocalKnowledgeService,
)
from oria.rag.snapshots import LocalRuleSnapshotStore
from oria.resources.loader import load_demo_data
from oria.storage.assortment import SQLiteAssortmentWorkflowRepository
from oria.storage.database import DatabaseResources
from oria.storage.platform import (
    SQLiteApprovalInvalidationRepository,
    SQLiteApprovalRepository,
    SQLiteIntegrationEventInboxRepository,
)
from oria.storage.repositories import (
    SQLiteCampaignDraftRepository,
    SQLiteCampaignLaunchRepository,
    SQLiteCampaignRepository,
    SQLiteCampaignRuleSnapshotRefRepository,
    SQLiteCouponBatchRepository,
    SQLiteEnrollmentWorkflowRepository,
    SQLiteMerchantRepository,
)
from oria.tools.assortment import (
    PublishConsumerPlacementTool,
    SendMerchantNotificationTool,
    SubmitAssortmentTool,
)
from oria.tools.builtin import QueryMerchantsTool, SearchCampaignRulesTool
from oria.tools.registry import ToolRegistry

RuntimeResourceFactory = Callable[[], AbstractAsyncContextManager[object]]


async def build_runtime(
    config: ResolvedRuntimeConfig | None = None,
    *,
    resource_factories: Sequence[RuntimeResourceFactory] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    trusted_actors: Sequence[Principal] | None = None,
    trusted_executors: Sequence[Principal] | None = None,
    confirmation_assignments: Mapping[str, str] | None = None,
    integration_event_subjects: Mapping[tuple[str, str], frozenset[str]] | None = None,
) -> RuntimeServices:
    """Build and seal one runtime, unwinding every startup resource on failure."""
    resolved = resolve_runtime_config() if config is None else config
    exit_stack = SealedAsyncExitStack()
    try:
        llm: LLMProvider
        if resolved.llm.provider == "mock":
            llm = DemoMockLLMProvider()
        elif resolved.llm.provider in {"deepseek", "kimi", "zhipu", "openai", "anthropic"}:
            if resolved.llm.base_url is None:
                raise ValueError(f"{resolved.llm.provider} profile requires a base_url")
            http_client = await exit_stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=resolved.llm.base_url,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                )
            )
            if resolved.llm.provider == "anthropic":
                llm = AnthropicProvider(resolved.llm, http_client)
            else:
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
        bm25_index = BM25Index()
        audit = PlatformAuditService(database_resources.platform_sessions, resolved)
        policy = LocalPolicyEngine(
            audit,
            trusted_actors=trusted_actors,
            trusted_executors=trusted_executors,
            confirmation_assignments=confirmation_assignments,
        )
        knowledge = LocalKnowledgeService(
            catalog=catalog,
            objects=objects,
            index=index,
            embedder=embedder,
            embedding_profile=embedding_projection,
            bm25_index=bm25_index,
        )
        bm25_retriever = AuthorizedBM25Retriever(
            catalog=catalog,
            index=bm25_index,
            knowledge=knowledge,
        )
        retriever = ConfigurableRetriever(
            mode="dense",
            dense=AuthorizedChromaRetriever(
                catalog=catalog,
                index=index,
                embedder=embedder,
                knowledge=knowledge,
            ),
            bm25=bm25_retriever,
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
        assortment_repository = SQLiteAssortmentWorkflowRepository(
            database_resources.business_sessions
        )
        approvals = ApprovalService(
            SQLiteApprovalRepository(database_resources.platform_sessions),
            policy,
            clock=clock,
            binding_reader=assortment_repository,
        )
        ledger = ExecutionLedger(database_resources.business_sessions, clock=clock)
        campaign_repository = SQLiteCampaignRepository(database_resources.business_sessions)
        rule_ref_repository = SQLiteCampaignRuleSnapshotRefRepository(
            database_resources.business_sessions
        )
        coupon_repository = SQLiteCouponBatchRepository(database_resources.business_sessions)
        enrollment_repository = SQLiteEnrollmentWorkflowRepository(
            database_resources.business_sessions
        )
        inbox_repository = SQLiteIntegrationEventInboxRepository(
            database_resources.platform_sessions
        )
        approval_invalidator = ApprovalBindingInvalidationConsumer(
            SQLiteApprovalInvalidationRepository(database_resources.platform_sessions)
        )
        product_catalog = InMemoryProductCatalogAdapter(
            {
                bundle.merchants.tenant_id: tuple(
                    ProductSnapshot(
                        product_ref=f"synthetic-product-{merchant.merchant_id}",
                        product_version="v1",
                        merchant_id=merchant.merchant_id,
                        source_ref=(f"synthetic://catalog/{merchant.merchant_id}/product/v1"),
                        captured_at=clock(),
                        category="餐饮套餐",
                        normalized_price=Decimal("100.00"),
                        currency="CNY",
                        normalized_title=f"合成夏季套餐 {merchant.merchant_id}",
                        keyword_labels=("夏季", "套餐"),
                        eligibility_facts={"available": True, "status": "available"},
                    )
                    for merchant in bundle.merchants.merchants
                )
            }
        )
        product_query = ProductQueryService(
            campaigns=campaign_repository,
            rule_refs=rule_ref_repository,
            rule_snapshots=rule_snapshots,
            catalog=product_catalog,
            eligibility=ProductEligibilityPolicy(),
            merchants=merchant_repository,
            merchant_eligibility=EligibilityPolicy(),
        )
        enrollment_service = EnrollmentService(
            campaigns=campaign_repository,
            rule_refs=rule_ref_repository,
            rule_snapshots=rule_snapshots,
            merchants=merchant_repository,
            repository=enrollment_repository,
            catalog=product_catalog,
            ledger=ledger,
            subjects=InMemoryConfirmationSubjectDirectory(
                {
                    (bundle.merchants.tenant_id, merchant.merchant_id): {
                        "merchant": merchant.merchant_id,
                        "sales": f"sales:{merchant.merchant_id}",
                        "sales_manager": f"sales-manager:{merchant.merchant_id}",
                    }
                    for merchant in bundle.merchants.merchants
                }
            ),
            clock=clock,
        )
        integration_events = IntegrationEventInboxService(
            inbox_repository,
            authorized_subjects=integration_event_subjects
            or {
                (bundle.merchants.tenant_id, "mock-merchant"): frozenset({"mock-merchant-adapter"}),
                (bundle.merchants.tenant_id, "mock-selection"): frozenset(
                    {"mock-selection-adapter"}
                ),
            },
            clock=clock,
        )
        enrollment_branches = EnrollmentBranchCoordinator(
            inbox=integration_events,
            enrollments=enrollment_service,
            approval_invalidator=approval_invalidator,
            clock=clock,
        )
        confirmations = ConfirmationService(
            repository=enrollment_repository,
            ledger=ledger,
            clock=clock,
        )
        coupon_links = CouponLinkService(
            repository=enrollment_repository,
            ledger=ledger,
            campaigns=campaign_repository,
            coupons=coupon_repository,
            rule_refs=rule_ref_repository,
            rule_snapshots=rule_snapshots,
            catalog=product_catalog,
            clock=clock,
        )
        assortment = AssortmentService(
            campaigns=campaign_repository,
            rule_refs=rule_ref_repository,
            rule_snapshots=rule_snapshots,
            repository=assortment_repository,
            ledger=ledger,
            approvals=approvals,
            assortment_adapter=InMemoryAssortmentAdapter(clock=clock),
            placement_adapter=InMemoryConsumerPlacementAdapter(clock=clock),
            notification_adapter=InMemoryMerchantNotificationAdapter(clock=clock),
            approval_invalidator=approval_invalidator,
            clock=clock,
        )
        selection_events = TrustedSelectionEventService(
            inbox_repository,
            assortment,
            clock=clock,
        )
        campaign_launch = DefaultCampaignLaunchService(
            SQLiteCampaignDraftRepository(database_resources.business_sessions),
            launches=SQLiteCampaignLaunchRepository(database_resources.business_sessions),
            approvals=approvals,
            ledger=ledger,
            coupon_adapter=InMemoryCouponBatchAdapter(clock=clock),
            recruitment_adapter=InMemoryRecruitmentAdapter(clock=clock),
            clock=clock,
        )
        scenario_a = DefaultScenarioAWorkflowService(
            campaign_launch=campaign_launch,
            approvals=approvals,
            products=product_query,
            enrollment_branches=enrollment_branches,
            confirmations=confirmations,
            coupon_links=coupon_links,
            assortment=assortment,
            selection_events=selection_events,
            integration_events=integration_events,
        )
        domain = DomainServiceRegistry(
            campaign_rules=campaign_rules,
            merchants=merchant_service,
            campaign_launch=campaign_launch,
            assortment=assortment,
            selection_events=selection_events,
            scenario_a=scenario_a,
        )

        tools = ToolRegistry(
            allowlist=frozenset(
                {
                    "search_campaign_rules",
                    "query_merchants",
                    "submit_assortment",
                    "publish_consumer_placement",
                    "send_merchant_notification",
                }
            )
        )
        tools.register(SearchCampaignRulesTool(retriever, rule_snapshots))
        tools.register(QueryMerchantsTool(rule_snapshots, merchant_service))
        tools.register(SubmitAssortmentTool(assortment))
        tools.register(PublishConsumerPlacementTool(assortment))
        tools.register(SendMerchantNotificationTool(assortment))
        guardrails: ServiceRegistry[Guardrail] = ServiceRegistry()
        nodes: ServiceRegistry[Node] = ServiceRegistry()
        agents: ServiceRegistry[object] = ServiceRegistry()
        ingress: ServiceRegistry[IngressAdapter] = ServiceRegistry()
        notifier: ServiceRegistry[Notifier] = ServiceRegistry()
        ingress.register("cli", LocalCLIIngressAdapter())
        agents.register("research_agent", build_research_graph(checkpointer=checkpointer))
        agents.register("scenario_a", build_scenario_a_graph(checkpointer=checkpointer))

        tools.seal()
        for registry in (guardrails, nodes, agents, ingress, notifier):
            registry.seal()

        runtime = RuntimeServices(
            config=resolved,
            policy=policy,
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
