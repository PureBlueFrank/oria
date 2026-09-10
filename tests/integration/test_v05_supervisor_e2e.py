"""Offline end-to-end coverage for supervisor to subagent handoff."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.graph.state import CompiledStateGraph

from oria.agent import SupervisorRunContext, initial_supervisor_state
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.orchestrator.checkpoint import checkpoint_config
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fixture_supervisor_dispatches_executes_and_collects_result(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "runtime")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="supervisor-session",
            thread_id="supervisor-thread",
            run_id="supervisor-run",
        )
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        graph = cast(CompiledStateGraph[Any, Any, Any, Any], ctx.agents.get("supervisor"))

        output = await graph.ainvoke(
            initial_supervisor_state(
                user_request="生成华东餐饮招商活动建议",
                effective_at="2026-07-15T00:00:00+08:00",
            ),
            config=checkpoint_config(ctx),
            context=SupervisorRunContext(ctx=ctx),
        )

        assert output["termination"] is None
        assert output["route_reason"] == "campaign_term"
        assert output["handoffs"][0]["subagent_name"] == "campaign_research"
        assert output["subagent_results"][0]["status"] == "completed"
        assert output["subagent_results"][0]["visible_tools"] == [
            "search_campaign_rules",
            "query_merchants",
        ]
        assert output["final_result"]["abstained"] is False
        assert len(output["final_result"]["recommended_merchants"]) == 10
        assert output["model_turns"] == 3
    finally:
        await runtime.aclose()
