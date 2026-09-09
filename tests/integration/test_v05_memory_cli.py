"""V0.5-T02 CLI lifecycle through the shared policy-authorized memory service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oria.cli import app
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.memory import PersistentMemory
from oria.permission.local import local_cli_executor, local_operator

pytestmark = pytest.mark.integration


def test_memory_cli_view_export_and_delete_are_redacted(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = resolve_runtime_config(environ={}, data_dir=data_dir)

    async def seed() -> str:
        await initialize_data(config)
        runtime = await build_runtime(config)
        try:
            ctx = runtime.new_context(
                actor=local_operator(),
                executor=local_cli_executor(),
                session_id="seed-session",
                thread_id="seed-thread",
                run_id="seed-run",
            )
            memory = runtime.memory
            assert isinstance(memory, PersistentMemory)
            item = await memory.save_fact(
                "偏好无糖咖啡; token=secret-value",
                ctx,
                provenance="user-explicit",
                confidence=0.9,
                sensitivity="low",
                opt_in=True,
            )
            return item.id
        finally:
            await runtime.aclose()

    item_id = asyncio.run(seed())

    runner = CliRunner()
    view = runner.invoke(
        app,
        ["memory", "view", "--output", "json", "--data-dir", str(data_dir)],
    )
    exported = runner.invoke(app, ["memory", "export", "--data-dir", str(data_dir)])
    deleted = runner.invoke(
        app,
        ["memory", "delete", item_id, "--output", "json", "--data-dir", str(data_dir)],
    )
    after = runner.invoke(
        app,
        ["memory", "view", "--output", "json", "--data-dir", str(data_dir)],
    )

    assert view.exit_code == exported.exit_code == deleted.exit_code == after.exit_code == 0
    assert "secret-value" not in view.stdout + exported.stdout + deleted.stdout
    assert "[REDACTED]" in view.stdout
    assert json.loads(exported.stdout)["data"]["items"][0]["id"] == item_id
    assert json.loads(deleted.stdout)["data"] == {
        "deleted": True,
        "memory_id": item_id,
        "schema_version": 1,
    }
    assert json.loads(after.stdout)["data"]["items"] == []
