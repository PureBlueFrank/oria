"""Deterministic regression coverage for the approved RAG suite."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.eval import (
    RagGoldenGateError,
    assert_rag_gates,
    load_rag_baseline,
    load_rag_gates,
    run_rag_eval,
)
from oria.rag.rerank import FixtureReranker

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "eval" / "datasets" / "rag" / "v1.manifest.json"
_BASELINE = _ROOT / "eval" / "baselines" / "rag" / "1.json"
_GATES = _ROOT / "eval" / "config" / "rag-gates.yaml"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_approved_rag_cases_meet_the_frozen_baseline(tmp_path: Path) -> None:
    gates_sha256 = _sha256(_GATES)
    lock_sha256 = _sha256(_ROOT / "uv.lock")
    config = resolve_runtime_config(
        environ={"ORIA_ENVIRONMENT": "test"},
        data_dir=tmp_path / "data",
    )
    report = await run_rag_eval(
        _MANIFEST,
        config=config,
        reranker=FixtureReranker(),
        reranker_profile="fixture",
        split="all",
        gates_sha256=gates_sha256,
        lock_sha256=lock_sha256,
    )
    baseline = load_rag_baseline(_BASELINE)
    gates = load_rag_gates(_GATES)

    assert_rag_gates(
        report,
        gates=gates,
        gates_sha256=gates_sha256,
        lock_sha256=lock_sha256,
        baseline=baseline,
    )

    dense = report.pipelines[0]
    regressed = report.model_copy(
        update={
            "pipelines": (
                dense.model_copy(
                    update={
                        "metrics": dense.metrics.model_copy(
                            update={"mrr": dense.metrics.mrr - 0.01}
                        )
                    }
                ),
                *report.pipelines[1:],
            )
        }
    )
    with pytest.raises(RagGoldenGateError, match="baseline dense metric failed: mrr"):
        assert_rag_gates(
            regressed,
            gates=gates,
            gates_sha256=gates_sha256,
            lock_sha256=lock_sha256,
            baseline=baseline,
        )
