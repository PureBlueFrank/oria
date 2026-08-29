"""Verify T07 prompt and official checkpoint behavior from an installed wheel."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from oria.agent import build_research_graph
from oria.eval import ScenarioAGates
from oria.orchestrator import open_tenant_sqlite_saver
from oria.prompts import PromptManager


class _CounterState(TypedDict):
    value: int


def _config(tenant_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "same-external-thread",
            "checkpoint_ns": "",
            "oria_tenant_id": tenant_id,
        }
    }


async def _verify(data_dir: Path) -> dict[str, object]:
    gates = ScenarioAGates(
        suite="scenario_a",
        dataset_version="1",
        required_metrics={
            "case_pass_rate": 1.0,
            "critical_pass_rate": 1.0,
            "outcome_accuracy": 1.0,
            "tool_sequence_accuracy": 1.0,
            "grounded_proposal_rate": 1.0,
        },
    )
    prompt = PromptManager().render(
        "merchant_selection",
        version=1,
        effective_at="2026-07-15T00:00:00+08:00",
        max_candidates=10,
    )
    if "CampaignProposalDraft v1" not in prompt:
        raise AssertionError("installed prompt resource was not rendered")
    if tuple(build_research_graph().get_graph().nodes) != (
        "__start__",
        "model",
        "tools",
        "validate",
        "__end__",
    ):
        raise AssertionError("installed research graph topology is invalid")
    async with open_tenant_sqlite_saver(data_dir / "checkpoints.sqlite3") as saver:
        builder = StateGraph(_CounterState)
        builder.add_node("increment", lambda state: {"value": state["value"] + 1})
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        graph = builder.compile(checkpointer=saver)
        await graph.ainvoke({"value": 1}, config=_config("tenant-a"))
        await graph.ainvoke({"value": 40}, config=_config("tenant-b"))
        state_a = await graph.aget_state(_config("tenant-a"))
        state_b = await graph.aget_state(_config("tenant-b"))
        if state_a.values != {"value": 2} or state_b.values != {"value": 41}:
            raise AssertionError("installed checkpoint adapter did not isolate tenants")
        if "oria_v1_" in repr((state_a.config, state_b.config)):
            raise AssertionError("checkpoint storage key leaked through the public API")
    return {
        "ok": True,
        "prompt_version": 1,
        "graph_nodes": ["model", "tools", "validate"],
        "checkpoint_tenant_isolation": True,
        "golden_contract": gates.dataset_version == "1",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_verify(args.data_dir)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
