"""Pull-request policy for versioned evaluation baseline and gate changes."""

from __future__ import annotations

from collections.abc import Iterable

_REQUIRED_LABEL = "eval-baseline-update"


class EvalBaselineUpdateError(RuntimeError):
    """Raised when protected eval evidence changes without explicit review intent."""


def assert_eval_baseline_update_policy(
    *,
    changed_paths: Iterable[str],
    labels: Iterable[str],
) -> None:
    protected = tuple(path for path in changed_paths if _is_protected(path))
    if protected and _REQUIRED_LABEL not in frozenset(labels):
        joined = ",".join(sorted(protected))
        raise EvalBaselineUpdateError(
            f"eval baseline/gate changes require {_REQUIRED_LABEL}: {joined}"
        )


def _is_protected(path: str) -> bool:
    normalized = path.removeprefix("./")
    return normalized.startswith("eval/baselines/") or normalized in {
        "eval/config/gates.yaml",
        "eval/config/rag-gates.yaml",
    }
