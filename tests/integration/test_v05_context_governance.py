"""Research-loop integration coverage for V0.5 context governance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from oria.agent import ResearchRunContext, build_research_graph, initial_research_state
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import Message
from oria.memory import ContextBudget, InMemoryMemory, estimate_tokens
from oria.permission.local import local_cli_executor, local_operator

pytestmark = pytest.mark.integration


class _CapturingFailureProvider:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def chat(self, messages: list[Message], *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.messages = messages
        raise RuntimeError("local fixture failure after capture")


@pytest.mark.asyncio
async def test_research_model_compresses_checkpoint_history_before_provider_call(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=tmp_path / "data"))
    try:
        budget = ContextBudget(max_context_tokens=2_000, reserve_tokens=200)
        memory = InMemoryMemory(budget)
        provider = _CapturingFailureProvider()
        object.__setattr__(runtime, "memory", memory)
        object.__setattr__(runtime, "llm", provider)
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="v05-session",
            thread_id="v05-thread",
            run_id="v05-run",
        )
        state = initial_research_state(
            user_request="Analyze the fixture evidence.",
            effective_at="2026-09-10T00:00:00+08:00",
        )
        old_messages = [
            Message(
                role="tool",
                tool_call_id=f"old-{index}",
                content=json.dumps(
                    {
                        "merchant_id": "merchant-042",
                        "amount": 128500,
                        "conclusion": "unit-price decline",
                        "details": "x" * 1_200,
                    }
                ),
            ).model_dump(mode="json")
            for index in range(8)
        ]
        state["messages"] = [*state["messages"], *old_messages]
        del state["fact_ledger"]

        result = await build_research_graph().ainvoke(
            state,
            context=ResearchRunContext(ctx=ctx),
        )

        facts = {entry["key"]: entry["value"] for entry in result["fact_ledger"]}
        assert result["termination"]["reason"] == "provider_failure"
        assert estimate_tokens(provider.messages) <= budget.message_token_limit
        assert len(result["messages"]) < len(state["messages"])
        assert facts["merchant_id"] == "merchant-042"
        assert facts["amount"] == 128500
        assert facts["conclusion"] == "unit-price decline"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_community_runtime_mounts_session_memory(tmp_path: Path) -> None:
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=tmp_path / "data"))
    try:
        assert isinstance(runtime.memory, InMemoryMemory)
    finally:
        await runtime.aclose()
