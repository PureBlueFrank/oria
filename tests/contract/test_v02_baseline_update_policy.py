"""Baseline update label and ownership contracts."""

from pathlib import Path

import pytest

from oria.eval import EvalBaselineUpdateError, assert_eval_baseline_update_policy

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]


def test_protected_baseline_change_requires_explicit_label() -> None:
    with pytest.raises(EvalBaselineUpdateError, match="eval-baseline-update"):
        assert_eval_baseline_update_policy(
            changed_paths=("eval/baselines/rag/1.json",),
            labels=(),
        )


def test_explicit_label_allows_protected_baseline_change() -> None:
    assert_eval_baseline_update_policy(
        changed_paths=("eval/config/rag-gates.yaml",),
        labels=("eval-baseline-update",),
    )


def test_unrelated_change_does_not_require_baseline_label() -> None:
    assert_eval_baseline_update_policy(
        changed_paths=("src/oria/cli.py",),
        labels=(),
    )


def test_eval_assets_have_a_codeowner() -> None:
    codeowners = (_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")

    assert "/eval/datasets/ @PureBlueFrank" in codeowners
    assert "/eval/baselines/ @PureBlueFrank" in codeowners
    assert "/eval/config/rag-gates.yaml @PureBlueFrank" in codeowners
