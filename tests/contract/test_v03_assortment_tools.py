"""Runtime contracts for the three T06 model tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data

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
