"""Runtime registration integration for V0.5-T03 guardrails."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_runtime_mounts_all_guardrails_before_sealing(tmp_path: Path) -> None:
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=tmp_path / "data"))
    try:
        assert runtime.guardrails.sealed is True
        assert set(runtime.guardrails) == {
            "input.prompt_injection",
            "input.rag_injection",
            "tool.authorization",
            "output.safety",
        }
        assert runtime.guardrails.get("input.prompt_injection").phase == "input"
        assert runtime.guardrails.get("input.rag_injection").phase == "input"
        assert runtime.guardrails.get("tool.authorization").phase == "tool"
        assert runtime.guardrails.get("output.safety").phase == "output"
    finally:
        await runtime.aclose()
