"""RAG evaluation harness integration without opening the frozen holdout."""

from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.eval import run_rag_eval
from oria.rag.rerank import FixtureReranker

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "eval" / "datasets" / "rag" / "v1.manifest.json"


@pytest.mark.asyncio
async def test_fixture_harness_compares_three_pipelines_on_development_split(
    tmp_path: Path,
) -> None:
    config = resolve_runtime_config(
        environ={"ORIA_ENVIRONMENT": "test"},
        data_dir=tmp_path / "data",
    )

    report = await run_rag_eval(
        _MANIFEST,
        config=config,
        reranker=FixtureReranker(),
        reranker_profile="fixture",
        split="development",
        require_human_review=False,
    )

    assert report.verification_level == "fixture"
    assert report.eval_fingerprint.startswith("sha256:")
    assert [pipeline.mode for pipeline in report.pipelines] == [
        "dense",
        "hybrid",
        "hybrid_rerank",
    ]
    for pipeline in report.pipelines:
        assert pipeline.metrics.evaluated_cases == 42
        assert pipeline.metrics.citation_hit_rate == 1.0
        assert len(pipeline.cases) == 42
