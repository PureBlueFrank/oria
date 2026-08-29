"""T10 required Live card for real DeepSeek Responses and pinned local BGE."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from oria._internal.target_selection import parse_targets
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.demo import DemoResult, execute_demo

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_BGE_REVISION = "a7ec18349c42fc774b0e86af26215e38a10fbe9d"


def _assert_live_result(result: DemoResult, secret: str) -> tuple[str, ...]:
    assert result.profile == "community+standard"
    assert result.validation.business_side_effect_free is True
    assert result.validation.forbidden_business_tables == ()
    assert result.validation.eligible_merchant_count == 10
    assert result.usage.model_turns >= 3
    assert result.usage.tool_calls_total == 2
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert [event.tool for event in result.events if event.type == "tool_completed"] == [
        "search_campaign_rules",
        "query_merchants",
    ]
    model_events = [
        event for event in result.events if event.type in {"model_completed", "model_failed"}
    ]
    request_ids = tuple(
        event.provider_request_id for event in model_events if event.provider_request_id
    )
    assert len(request_ids) == len(model_events)
    assert len(model_events) == result.usage.model_turns
    assert len(set(request_ids)) == len(request_ids)
    assert {event.provider_model for event in model_events} == {"deepseek-v4-flash"}
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert secret not in report
    return request_ids


async def test_deepseek_bge_scenario_a_is_repeatable_and_side_effect_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = parse_targets(os.getenv("ORIA_LIVE_TARGETS"), {"deepseek"}, "live")
    if "deepseek" not in targets:
        pytest.skip("deepseek was not selected")
    secret = os.getenv("DEEPSEEK_API_KEY")
    if not secret:
        pytest.fail("blocked: DEEPSEEK_API_KEY is required for selected deepseek target")

    data_dir = Path(os.getenv("ORIA_T10_DATA_DIR", str(tmp_path / "t10-live-data")))
    environ = dict(os.environ)
    environ.update(
        {
            "ORIA_RUNTIME_PROFILE": "standard",
            "ORIA_LLM_PROFILE": "deepseek",
            "ORIA_EMBEDDING_PROFILE": "bge",
        }
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    config = resolve_runtime_config(
        runtime_profile="standard",
        llm_profile="deepseek",
        data_dir=data_dir,
        environ=environ,
    )
    assert config.llm.api_dialect == "responses"
    assert config.llm.model == "deepseek-v4-flash"
    assert config.embedding.model == "BAAI/bge-small-zh-v1.5"
    assert config.embedding.revision == _BGE_REVISION
    assert config.embedding.trust_remote_code is False

    first_initialization = await initialize_data(config)
    first_runtime = await build_runtime(config)
    async with first_runtime:
        first = await execute_demo(first_runtime, first_initialization)

    second_initialization = await initialize_data(config)
    second_runtime = await build_runtime(config)
    async with second_runtime:
        second = await execute_demo(second_runtime, second_initialization)

    first_ids = _assert_live_result(first, secret)
    second_ids = _assert_live_result(second, secret)
    assert first_initialization.merchants_inserted == 12
    assert second_initialization.merchants_inserted == 0
    assert first.ingestion.idempotent is False
    assert second.ingestion.idempotent is True
    assert set(first_ids).isdisjoint(second_ids)
