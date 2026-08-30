from pathlib import Path

import pytest
from typer.testing import CliRunner

from oria import __version__
from oria.cli import app

pytestmark = pytest.mark.unit


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


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
