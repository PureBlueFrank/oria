"""Runtime contracts for the three T06 model tools."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.permission.local import local_cli_executor, local_operator

pytestmark = pytest.mark.contract

_T06_TOOLS = {
    "submit_assortment",
    "publish_consumer_placement",
    "send_merchant_notification",
}


@pytest.mark.asyncio
async def test_runtime_registers_strict_redacted_t06_tools_and_separate_event_service(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        specs = {spec.name: spec for spec in runtime.tools.specs()}
        assert specs.keys() >= _T06_TOOLS
        for name in _T06_TOOLS:
            tool = runtime.tools.get(name)
            assert tool.schema_version == specs[name].schema_version == 1
            assert specs[name].strict is True
            assert specs[name].json_schema["additionalProperties"] is False
            assert tool.result_schema["additionalProperties"] is False
            assert tool.policy.side_effect is True

        notification_schema = runtime.tools.get("send_merchant_notification").result_schema
        notification_properties = notification_schema["properties"]
        assert "merchant_id" not in notification_properties
        assert "tenant_id" not in notification_properties
        assert "rejected_reasons" not in notification_properties
        assert "selection_event" not in runtime.tools.allowlist
        assert runtime.domain.assortment is not None
        assert runtime.domain.selection_events is not None
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_side_effect_tool_checkpoint_is_trusted_context_only_and_fails_closed(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    await initialize_data(config)
    runtime = await build_runtime(config)
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="checkpoint-session",
        thread_id="checkpoint-thread",
        run_id="run-is-not-checkpoint",
    )
    try:
        tool = runtime.tools.get("submit_assortment")
        assert "checkpoint_id" not in tool.json_schema["properties"]
        with pytest.raises(RuntimeError, match="trusted checkpoint_id"):
            await tool.run(
                {
                    "campaign_id": "campaign-a",
                    "enrollment_item_ids": ["item-a"],
                    "assortment_policy_ref": "policy-a",
                    "assortment_policy_version": "v1",
                    "idempotency_key": "request-a",
                },
                ctx,
            )
    finally:
        await runtime.aclose()

    with sqlite3.connect(config.data_paths.business_db) as connection:
        execution_count = connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()
    assert execution_count == (0,)
