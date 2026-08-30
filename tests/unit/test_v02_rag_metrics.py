"""RAG metric aggregation regression tests."""

import pytest

from oria.eval.rag import RagCaseResult, RagDatasetError, _metrics

pytestmark = pytest.mark.unit


def test_citation_hit_rate_counts_each_returned_citation() -> None:
    cases = [
        RagCaseResult(
            case_id="rag-v1-001",
            split="development",
            critical=True,
            relevant_rank=1,
            returned_chunk_ids=("chunk-1", "chunk-2"),
            returned_rule_categories=("basic", "basic"),
            valid_citation_count=1,
            citations_valid=False,
            latency_ms=1.0,
            passed=False,
        ),
        RagCaseResult(
            case_id="rag-v1-002",
            split="development",
            critical=False,
            relevant_rank=1,
            returned_chunk_ids=("chunk-3",),
            returned_rule_categories=("basic",),
            valid_citation_count=1,
            citations_valid=True,
            latency_ms=2.0,
            passed=True,
        ),
    ]

    metrics = _metrics(cases)

    assert metrics.citation_hit_rate == pytest.approx(2 / 3)


def test_metrics_rejects_a_split_without_critical_cases() -> None:
    case = RagCaseResult(
        case_id="rag-v1-001",
        split="holdout",
        critical=False,
        relevant_rank=1,
        returned_chunk_ids=("chunk-1",),
        returned_rule_categories=("basic",),
        valid_citation_count=1,
        citations_valid=True,
        latency_ms=1.0,
        passed=True,
    )

    with pytest.raises(RagDatasetError, match="selected no critical cases"):
        _metrics([case])
