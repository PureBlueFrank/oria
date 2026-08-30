"""CI and Nightly workflow contracts for the approved RAG suite."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]


def test_pr_golden_runs_the_frozen_rag_suite() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "uv run python scripts/run_rag_golden.py" in workflow
    assert "scripts/check_eval_baseline_update.py" in workflow
    assert "ORIA_EVAL_PR_LABELS" in workflow
    assert "path: .artifacts/eval/rag_v1.json" in workflow
    assert "if-no-files-found: error" in workflow


def test_nightly_is_scheduled_and_runs_preflight_before_bounded_live_requests() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "eval-nightly.yml").read_text(encoding="utf-8")

    assert 'cron: "0 18 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "secrets.DEEPSEEK_API_KEY" in workflow
    assert "scripts/preflight_eval_nightly.py" in workflow
    assert workflow.index("scripts/preflight_eval_nightly.py") < workflow.index(
        "scripts/run_provider_live.py"
    )
    assert workflow.index("scripts/run_provider_live.py") < workflow.index(
        "scripts/run_eval_nightly.py"
    )
    assert "if: always()" in workflow
    assert "nightly-preflight.json" in workflow
    assert "provider-live.json" in workflow
    assert "nightly-run.json" in workflow
