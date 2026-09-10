"""Local human CLI presentation for the Scenario A happy path."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oria.cli import app

pytestmark = pytest.mark.integration


def test_workflow_human_output_localizes_rules_enrollment_and_duplicate_error(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    data_dir = tmp_path / "workflow-ux"
    common = ["--data-dir", str(data_dir)]

    started = runner.invoke(
        app,
        [
            "workflow",
            "start",
            "--thread-id",
            "ux-thread",
            "--campaign-id",
            "ux-campaign",
            "--request",
            "创建夏季餐饮招商活动",
            *common,
        ],
    )
    assert started.exit_code == 0
    assert "招商活动自动化 · 第 2/10 阶段" in started.stdout
    assert "夏季餐饮活动模板" in started.stdout
    assert "synthetic-summer-dining" not in started.stdout
    approval_match = re.search(r"approval_[0-9a-f]+", started.stdout)
    assert approval_match is not None

    duplicate = runner.invoke(
        app,
        [
            "workflow",
            "start",
            "--thread-id",
            "ux-thread-retry",
            "--campaign-id",
            "ux-campaign",
            *common,
        ],
    )
    assert duplicate.exit_code == 1
    assert "活动 `ux-campaign` 已存在" in duplicate.stderr
    assert "请换一个新的 `--campaign-id`" in duplicate.stderr
    assert "oria workflow resume" in duplicate.stderr

    duplicate_json = runner.invoke(
        app,
        [
            "workflow",
            "start",
            "--thread-id",
            "ux-thread-json-retry",
            "--campaign-id",
            "ux-campaign",
            "--output",
            "json",
            *common,
        ],
    )
    assert duplicate_json.exit_code == 1
    assert json.loads(duplicate_json.stdout) == {
        "error": {
            "code": "workflow_operation_failed",
            "message": "campaign draft persistence failed",
        },
        "ok": False,
    }

    approved = runner.invoke(
        app,
        [
            "approval",
            "approve",
            "--thread-id",
            "ux-thread",
            "--approval-id",
            approval_match.group(),
            *common,
        ],
    )
    assert approved.exit_code == 0
    assert "招商活动自动化 · 第 4/10 阶段" in approved.stdout
    assert "流程进度" in approved.stdout
    assert "10. 通知商家并闭环" in approved.stdout
    assert "下一步命令: oria mock window-close" in approved.stdout

    enrolled = runner.invoke(
        app,
        [
            "mock",
            "enrollment",
            "--thread-id",
            "ux-thread",
            "--source-event-id",
            "ux-enrollment-event",
            "--merchant-id",
            "demo-m001",
            "--product-ref",
            "synthetic-product-demo-m001",
            *common,
        ],
    )
    assert enrolled.exit_code == 0
    assert "报名汇总" in enrolled.stdout
    assert "当前已报名 1 家商家, 已圈选 1 个商品" in enrolled.stdout
    assert "报名明细" in enrolled.stdout
    assert "虚构食坊一号 (demo-m001)" in enrolled.stdout
    assert "synthetic-product-demo-m001" in enrolled.stdout
    assert "商家自主报名" in enrolled.stdout
    assert "2026-07-10T04:00:00Z" in enrolled.stdout
    assert "商家候选" not in enrolled.stdout
    assert "…" not in enrolled.stdout
