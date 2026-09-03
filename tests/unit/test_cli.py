from pathlib import Path
from typing import cast

import pytest
from click import Group
from typer.main import get_command
from typer.testing import CliRunner

import oria.cli as cli_module
from oria import __version__
from oria.cli import app

pytestmark = pytest.mark.unit


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_scenario_a_cli_exposes_start_resume_approval_and_mock_events() -> None:
    root = cast(Group, get_command(app))

    assert "chat" in root.commands
    workflow = cast(Group, root.commands["workflow"])
    approval = cast(Group, root.commands["approval"])
    mock = cast(Group, root.commands["mock"])
    assert set(workflow.commands) == {"start", "resume"}
    assert set(approval.commands) == {"approve", "reject"}
    assert set(mock.commands) == {
        "enrollment",
        "window-close",
        "selection-decision",
        "selection-complete",
    }


def test_bare_oria_non_tty_prints_help_without_starting_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "_stdio_is_tty", lambda: False)

    def fail_if_called(**values: object) -> None:
        raise AssertionError("chat must not start in a non-TTY environment")

    monkeypatch.setattr(cli_module, "_run_chat_command", fail_if_called)
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "chat" in result.output


def test_bare_oria_enters_chat_only_when_stdin_and_stdout_are_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    monkeypatch.setattr(cli_module, "_stdio_is_tty", lambda: True)

    def record_call(**values: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli_module, "_run_chat_command", record_call)
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert called is True


def test_explicit_chat_runs_in_non_tty_input_and_exits_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["chat", "--data-dir", str(tmp_path / "data")],
        input="/quit\n",
    )

    assert result.exit_code == 0
    assert "Oria chat 已启动" in result.output
    assert "可信本地主体" in result.output
    assert "已退出 Oria chat" in result.output


def test_scenario_a_commands_expose_optional_runtime_profile_overrides() -> None:
    command_paths = {
        "workflow": ("start", "resume"),
        "approval": ("approve", "reject"),
        "mock": ("enrollment", "window-close", "selection-decision", "selection-complete"),
    }
    expected = {"--runtime-profile", "--llm-profile", "--embedding-profile"}

    for group_name, command_names in command_paths.items():
        for command_name in command_names:
            result = CliRunner().invoke(app, [group_name, command_name, "--help"])
            assert result.exit_code == 0
            assert all(option in result.output for option in expected)


def test_rag_eval_fails_closed_before_unreviewed_dataset_runs(
    tmp_path: Path,
    pending_rag_manifest: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "rag",
            "--manifest",
            str(pending_rag_manifest),
            "--eval-config",
            str(root / "eval" / "config" / "rag.yaml"),
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )

    assert result.exit_code == 2
    assert "pending actual human review" in result.output


def test_rag_eval_requires_gate_and_lock_identity_together(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "rag",
            "--manifest",
            str(root / "eval" / "datasets" / "rag" / "v1.manifest.json"),
            "--eval-config",
            str(root / "eval" / "config" / "rag.yaml"),
            "--data-dir",
            str(tmp_path / "data"),
            "--gates",
            str(root / "eval" / "config" / "rag-gates.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "gates and dependency lock must be bound together" in result.output
