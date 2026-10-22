"""Input-adapter E2E-F; no durable Job runner, claim or resume is implied."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from tests.integration.test_v04_attribution_agent import _AdaptiveAttributionProvider
from tests.unit.test_v06_job_inputs import context, job, template

from oria.agent import ResearchRunContext, attribution_research_limits, build_attribution_graph
from oria.analytics.query import AnalyticsQueryStore
from oria.config import resolve_runtime_config
from oria.core.jobs import Job, JobSubmission, submission_hash
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.eval.attribution_data import generate_attribution_fixture
from oria.orchestrator.checkpoint import checkpoint_config
from oria.orchestrator.job_inputs import attribution_job_input, campaign_job_input
from oria.orchestrator.local_executor import FIXTURE_NOW, campaign_admin
from oria.permission.local import local_cli_executor
from oria.rag.demo import demo_rule_document
from oria.tools.analytics import build_attribution_tool_registry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [1, 7, 10])
async def test_campaign_job_input_reaches_existing_graph_approval_with_owned_metadata(
    tmp_path, maximum
):
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(
        config,
        clock=lambda: FIXTURE_NOW,
        trusted_actors=(campaign_admin(),),
        trusted_executors=(local_cli_executor(),),
    )
    async with runtime:
        ctx = context(runtime)
        value = job(ctx, max_candidates=maximum)
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        state = campaign_job_input(value, ctx, template=template())
        graph = runtime.agents.get("scenario_a")
        result = await graph.ainvoke(state, config=checkpoint_config(ctx), context=ctx)
        assert result["__interrupt__"][0].value["kind"] == "launch_approval"
        assert result["meta"] == state["meta"]
        draft = result["results"]["draft"].updates["draft"]
        assert draft["campaign"]["campaign_id"] == "campaign_" + "4" * 32
        assert (
            0 < result["results"]["draft"].updates["merchant_result"]["returned_count"] <= maximum
        )
        assert value.status == "queued"  # Only the input contract was exercised.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,expected_tool,expected_outcome",
    [
        ("full_service", "query_activity", "attributed"),
        ("quick_service", "query_market_overview", "insufficient"),
    ],
)
async def test_attribution_job_input_retains_dynamic_research_and_abstention(
    tmp_path, category, expected_tool, expected_outcome
):
    database = tmp_path / "analytics.db"
    generate_attribution_fixture(database, tmp_path / "evaluation-only" / "labels.db")
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    provider = _AdaptiveAttributionProvider(category)
    async with await build_runtime(config, trusted_actors=(campaign_admin(),)) as runtime:
        registry = build_attribution_tool_registry(AnalyticsQueryStore(database), runtime.retriever)
        # Fixture provider/registry injection, as in the existing Scenario B E2E-F.
        object.__setattr__(runtime, "llm", provider)
        object.__setattr__(runtime, "tools", registry)
        ctx = context(runtime)
        original = job(ctx, "attribution")
        submission = JobSubmission.model_validate(
            original.submission.model_dump()
            | {"spec": original.submission.spec.model_dump() | {"category": category}}
        )
        value = Job.model_validate(
            original.model_dump()
            | {"submission": submission, "request_hash": submission_hash(submission)}
        )
        graph = build_attribution_graph(checkpointer=InMemorySaver())
        result = await graph.ainvoke(
            attribution_job_input(value, ctx),
            config=checkpoint_config(ctx),
            context=ResearchRunContext(ctx=ctx, limits=attribution_research_limits()),
        )
        assert result["termination"] is None
        assert result["conclusion"]["outcome"] == expected_outcome
        assert provider.selected_after_funnel == expected_tool
        assert "save_memory" not in provider.visible_tools[0]
