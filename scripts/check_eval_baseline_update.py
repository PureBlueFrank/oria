"""Reject pull requests that silently change an eval baseline or gate."""

from __future__ import annotations

import json
import os
import subprocess

from oria.eval import EvalBaselineUpdateError, assert_eval_baseline_update_policy


def _changed_paths(base_sha: str, head_sha: str) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, head_sha],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvalBaselineUpdateError("unable to resolve pull-request changes") from exc
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def main() -> int:
    base_sha = os.getenv("ORIA_EVAL_BASE_SHA", "").strip()
    head_sha = os.getenv("ORIA_EVAL_HEAD_SHA", "").strip()
    if not base_sha or not head_sha:
        print(json.dumps({"ok": False, "error": "pull-request SHAs are required"}))
        return 2
    try:
        labels_raw = json.loads(os.getenv("ORIA_EVAL_PR_LABELS", "[]"))
        if not isinstance(labels_raw, list) or any(
            not isinstance(item, str) for item in labels_raw
        ):
            raise EvalBaselineUpdateError("pull-request labels must be a JSON string list")
        changed = _changed_paths(base_sha, head_sha)
        assert_eval_baseline_update_policy(changed_paths=changed, labels=labels_raw)
    except (json.JSONDecodeError, EvalBaselineUpdateError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "changed_file_count": len(changed)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
