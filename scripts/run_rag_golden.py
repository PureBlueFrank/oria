"""Run the approved deterministic RAG suite and enforce its frozen baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path

from oria.config import resolve_runtime_config
from oria.eval import (
    RagGoldenGateError,
    assert_rag_gates,
    create_rag_baseline,
    load_rag_baseline,
    load_rag_gates,
    run_rag_eval,
    write_value_model,
)
from oria.rag.rerank import FixtureReranker


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RagGoldenGateError(f"required identity file is unavailable: {path.name}") from exc


async def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "eval" / "datasets" / "rag" / "v1.manifest.json"
    baseline_path = root / "eval" / "baselines" / "rag" / "1.json"
    gates_path = root / "eval" / "config" / "rag-gates.yaml"
    gates_sha256 = _sha256(gates_path)
    lock_sha256 = _sha256(root / "uv.lock")
    output_path = Path(args.output)
    gates = load_rag_gates(gates_path)
    with tempfile.TemporaryDirectory(prefix="oria-rag-golden-") as temporary:
        config = resolve_runtime_config(
            environ={"ORIA_ENVIRONMENT": "test"},
            data_dir=Path(temporary) / "data",
        )
        report = await run_rag_eval(
            manifest,
            config=config,
            reranker=FixtureReranker(),
            reranker_profile="fixture",
            split="all",
            gates_sha256=gates_sha256,
            lock_sha256=lock_sha256,
        )
    if args.create_baseline:
        if baseline_path.exists():
            raise RagGoldenGateError("refusing to overwrite an existing RAG baseline")
        assert_rag_gates(
            report,
            gates=gates,
            gates_sha256=gates_sha256,
            lock_sha256=lock_sha256,
        )
        baseline = create_rag_baseline(report, created_at=datetime.now().astimezone())
        write_value_model(baseline_path, baseline)
    else:
        baseline = load_rag_baseline(baseline_path)
        assert_rag_gates(
            report,
            gates=gates,
            gates_sha256=gates_sha256,
            lock_sha256=lock_sha256,
            baseline=baseline,
        )
    write_value_model(output_path, report)
    print(
        json.dumps(
            {
                "ok": True,
                "suite": report.suite,
                "dataset_version": report.dataset_version,
                "dataset_sha256": report.dataset_sha256,
                "eval_fingerprint": report.eval_fingerprint,
                "pipelines": {
                    pipeline.mode: pipeline.metrics.model_dump(mode="json")
                    for pipeline in report.pipelines
                },
                "output": str(output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=".artifacts/eval/rag_v1.json",
        help="Path for the deterministic per-case report.",
    )
    parser.add_argument(
        "--create-baseline",
        action="store_true",
        help="Create the first baseline; refuses to overwrite an existing file.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except (RagGoldenGateError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
