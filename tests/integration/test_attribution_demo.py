"""Offline application and CLI coverage for the Scenario B attribution demo."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oria.attribution_demo import AttributionAskResult
from oria.cli import app
from oria.presentation.attribution import render_attribution

pytestmark = pytest.mark.integration

_DEFAULT_QUESTION = "为什么 2026-08-31 华东正餐招商核销转化率明显下降?"


def _reject_network(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("attribution Mock replay attempted a network connection")


def _invoke_json(data_dir: Path, *arguments: str) -> AttributionAskResult:
    result = CliRunner().invoke(
        app,
        ["attribution", "ask", *arguments, "--output", "json", "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.output
    return AttributionAskResult.model_validate(json.loads(result.stdout))


def test_default_mock_replay_is_offline_isolated_and_human_explanatory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket.socket, "connect", _reject_network)
    monkeypatch.setenv("ORIA_LLM_PROFILE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-read")
    data_dir = tmp_path / "demo-data"

    first = _invoke_json(data_dir)
    second = _invoke_json(data_dir)

    assert first.mode == "mock_replay"
    assert first.environment_source == "clean_mock"
    assert first.runtime_environment == "development"
    assert first.provider == "mock"
    assert first.case_id == first.run_id == "sb-v1-001"
    assert first.user_question is None
    assert first.question_asked == _DEFAULT_QUESTION
    assert first.match_method == "default_case_id"
    assert first.conclusion is not None
    assert first.conclusion.outcome == "attributed"
    assert first.termination is None
    assert tuple(item.tool_name for item in first.tool_trace) == (
        "query_funnel",
        "query_activity",
        "query_market_overview",
    )
    assert first.fixture_dir != second.fixture_dir
    assert first.report_path != second.report_path
    assert Path(first.report_path).is_file()
    assert Path(first.fixture_dir, "analytics.db").is_file()
    assert Path(first.fixture_dir, "evaluation-only", "labels.db").is_file()

    reports_root = (data_dir / "reports-tmp").resolve()
    written_files = tuple(path for path in data_dir.rglob("*") if path.is_file())
    assert written_files
    assert all(reports_root in path.resolve().parents for path in written_files)

    rendered = render_attribution(first)
    for phrase in (
        "离线 Mock 回放",
        "user_question",
        "question_asked",
        "做了什么",
        "为什么做",
        "依据什么",
        "下一步为什么继续",
        "最终结果：归因",
        "工具细节（补充）",
    ):
        assert phrase in rendered


def test_free_question_uses_only_normalized_exact_matching(tmp_path: Path) -> None:
    question = f"  {_DEFAULT_QUESTION}\n"

    result = _invoke_json(tmp_path / "question-data", question)

    assert result.case_id == "sb-v1-001"
    assert result.user_question == question
    assert result.question_asked == _DEFAULT_QUESTION
    assert result.match_method == "normalized_question_exact"


@pytest.mark.parametrize(
    ("case_id", "outcome"),
    [("sb-v1-015", "insufficient"), ("sb-v1-020", "conflicting")],
)
def test_mock_replay_exposes_abstain_and_conflict_outcomes(
    tmp_path: Path,
    case_id: str,
    outcome: str,
) -> None:
    result = _invoke_json(tmp_path / case_id, "--case-id", case_id)

    assert result.conclusion is not None
    assert result.conclusion.outcome == outcome
    if outcome == "insufficient":
        assert result.conclusion.abstained is True
        assert result.conclusion.requested_data
    else:
        assert result.conclusion.abstained is False
        assert len(result.conclusion.hypotheses) >= 2


@pytest.mark.parametrize(
    ("arguments", "error_code"),
    [
        (("--case-id", "sb-v1-031"), "holdout_forbidden"),
        (("按 merchant_id 维度拆分华东正餐漏斗数据",), "holdout_forbidden"),
        (("--case-id", "sb-v1-999"), "unknown_case"),
        (("这个问题不在开发案例中",), "unknown_question"),
    ],
)
def test_holdout_and_unknown_selections_fail_closed_without_artifacts(
    tmp_path: Path,
    arguments: tuple[str, ...],
    error_code: str,
) -> None:
    data_dir = tmp_path / error_code
    result = CliRunner().invoke(
        app,
        [
            "attribution",
            "ask",
            *arguments,
            "--output",
            "json",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == error_code
    assert not data_dir.exists()
