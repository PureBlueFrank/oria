"""Versioned RAG dataset loader and deterministic/community evaluation harness."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Literal, Self

import yaml
from pydantic import Field, model_validator

from oria.config.models import ResolvedRuntimeConfig
from oria.core.protocols import Reranker
from oria.core.runtime import build_runtime
from oria.core.types import CitationBlock, ValueModel
from oria.data import initialize_data
from oria.eval.datasets import GoldenReview, HumanReviewRequired
from oria.permission.local import local_cli_executor, local_operator
from oria.rag.demo import demo_rule_document
from oria.rag.models import RuleCategory
from oria.rag.pipeline import ConfigurableRetriever, RetrievalMode

if TYPE_CHECKING:
    from oria.core.context import Context

_DATASET_FILE_PATTERN = r"^v[1-9][0-9]*\.jsonl$"
_PIPELINES: tuple[RetrievalMode, ...] = ("dense", "hybrid", "hybrid_rerank")


class RagDatasetError(ValueError):
    """Raised when the RAG dataset or manifest violates its frozen contract."""


class RagCase(ValueModel):
    case_id: str = Field(pattern=r"^rag-v[1-9][0-9]*-[0-9]{3}$")
    schema_version: Literal[1] = 1
    split: Literal["development", "holdout"]
    critical: bool
    query: str = Field(min_length=2, max_length=500)
    expected_document_id: str = Field(min_length=1)
    expected_document_version: str = Field(min_length=1)
    expected_rule_category: RuleCategory
    k: int = Field(default=3, ge=1, le=10)
    review: GoldenReview


class RagManifest(ValueModel):
    suite: Literal["rag"]
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    schema_version: Literal[1] = 1
    source: Literal["synthetic"]
    contains_real_entities: Literal[False]
    license: str = Field(min_length=1)
    generator_seed: str = Field(min_length=1)
    case_count: int = Field(ge=60)
    development_case_count: int = Field(ge=1)
    holdout_case_count: int = Field(ge=1)
    critical_case_count: int = Field(ge=1)
    development_critical_case_count: int = Field(ge=1)
    holdout_critical_case_count: int = Field(ge=1)
    dataset_file: str = Field(pattern=_DATASET_FILE_PATTERN)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_document_id: str
    source_document_version: str
    review_status: Literal["pending_human_review", "approved"]
    human_review_complete: bool
    holdout_frozen: bool
    baseline_created: bool

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        approved = self.review_status == "approved"
        if self.human_review_complete != approved or self.holdout_frozen != approved:
            raise ValueError("RAG review and holdout freeze fields disagree")
        if self.baseline_created and not approved:
            raise ValueError("RAG baseline cannot precede human review")
        if self.development_case_count + self.holdout_case_count != self.case_count:
            raise ValueError("RAG split counts must equal the case count")
        if (
            self.development_critical_case_count + self.holdout_critical_case_count
            != self.critical_case_count
        ):
            raise ValueError("RAG split critical counts must equal the critical-case count")
        return self


class RagDataset(ValueModel):
    manifest: RagManifest
    cases: tuple[RagCase, ...]


class RagCommunityModelConfig(ValueModel):
    model: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    trust_remote_code: Literal[False] = False
    license: str = Field(min_length=1)


class RagEvalConfig(ValueModel):
    suite: Literal["rag"]
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    embedding: RagCommunityModelConfig
    reranker: RagCommunityModelConfig


class RagCaseResult(ValueModel):
    case_id: str
    split: Literal["development", "holdout"]
    critical: bool
    relevant_rank: int | None = Field(default=None, ge=1)
    returned_chunk_ids: tuple[str, ...]
    returned_rule_categories: tuple[str, ...]
    valid_citation_count: int = Field(ge=0)
    citations_valid: bool
    latency_ms: float = Field(ge=0)
    passed: bool

    @model_validator(mode="after")
    def validate_citation_counts(self) -> Self:
        returned = len(self.returned_chunk_ids)
        if self.valid_citation_count > returned:
            raise ValueError("valid citation count exceeds returned chunks")
        if self.citations_valid != (self.valid_citation_count == returned):
            raise ValueError("citation validity disagrees with the per-citation count")
        return self


class RagMetrics(ValueModel):
    evaluated_cases: int = Field(ge=1)
    recall_at_k: float = Field(ge=0, le=1)
    recall_ci95_low: float = Field(ge=0, le=1)
    recall_ci95_high: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    citation_hit_rate: float = Field(ge=0, le=1)
    critical_pass_rate: float = Field(ge=0, le=1)
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)


class RagPipelineReport(ValueModel):
    mode: RetrievalMode
    metrics: RagMetrics
    cases: tuple[RagCaseResult, ...]


class RagEvalReport(ValueModel):
    suite: Literal["rag"] = "rag"
    runner_version: Literal["rag_v1"] = "rag_v1"
    dataset_version: str
    dataset_sha256: str
    split: Literal["development", "holdout", "all"]
    verification_level: Literal["fixture", "community"]
    embedding_profile: str
    reranker_profile: str
    config_fingerprint: str
    gates_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lock_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    eval_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    executed_at: datetime
    pipelines: tuple[RagPipelineReport, ...]


class RagQualityMetrics(ValueModel):
    recall_at_k: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    citation_hit_rate: float = Field(ge=0, le=1)
    critical_pass_rate: float = Field(ge=0, le=1)


class RagBaselineCase(ValueModel):
    case_id: str
    split: Literal["development", "holdout"]
    critical: bool
    relevant_rank: int | None = Field(default=None, ge=1)
    returned_rule_categories: tuple[str, ...]
    citations_valid: bool
    passed: bool


class RagBaselinePipeline(ValueModel):
    mode: RetrievalMode
    metrics: RagQualityMetrics
    cases: tuple[RagBaselineCase, ...]


class RagBaseline(ValueModel):
    suite: Literal["rag"] = "rag"
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_version: Literal["rag_v1"] = "rag_v1"
    split: Literal["all"] = "all"
    verification_level: Literal["fixture"] = "fixture"
    embedding_profile: Literal["fixture"] = "fixture"
    reranker_profile: Literal["fixture"] = "fixture"
    gates_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eval_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    pipelines: tuple[RagBaselinePipeline, ...]


class RagGates(ValueModel):
    suite: Literal["rag"]
    dataset_version: str
    allowed_regression: Literal[0] = 0
    required_metrics: dict[RetrievalMode, RagQualityMetrics]

    @model_validator(mode="after")
    def validate_pipelines(self) -> Self:
        if set(self.required_metrics) != set(_PIPELINES):
            raise ValueError("RAG gates must configure all three pipelines")
        return self


class RagGoldenGateError(RuntimeError):
    """Raised when the deterministic RAG report violates its frozen gate."""


def load_rag_dataset(
    manifest_path: Path,
    *,
    require_human_review: bool = True,
) -> RagDataset:
    try:
        manifest = RagManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RagDatasetError("RAG manifest is unavailable or invalid") from exc
    dataset_path = manifest_path.parent / manifest.dataset_file
    try:
        payload = dataset_path.read_bytes()
    except OSError as exc:
        raise RagDatasetError("RAG dataset is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != manifest.dataset_sha256:
        raise RagDatasetError("RAG dataset integrity check failed")
    cases: list[RagCase] = []
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                raise RagDatasetError("RAG dataset contains a blank line")
            cases.append(RagCase.model_validate_json(line))
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, RagDatasetError):
            raise
        raise RagDatasetError("RAG dataset contains an invalid case") from exc
    if len(cases) != manifest.case_count or len({case.case_id for case in cases}) != len(cases):
        raise RagDatasetError("RAG case count or identity is invalid")
    split_counts = {
        split: sum(case.split == split for case in cases) for split in ("development", "holdout")
    }
    if split_counts != {
        "development": manifest.development_case_count,
        "holdout": manifest.holdout_case_count,
    }:
        raise RagDatasetError("RAG dataset split counts are invalid")
    if sum(case.critical for case in cases) != manifest.critical_case_count:
        raise RagDatasetError("RAG critical-case count is invalid")
    split_critical_counts = {
        split: sum(case.split == split and case.critical for case in cases)
        for split in ("development", "holdout")
    }
    if split_critical_counts != {
        "development": manifest.development_critical_case_count,
        "holdout": manifest.holdout_critical_case_count,
    }:
        raise RagDatasetError("RAG split critical-case counts are invalid")
    if any(
        case.expected_document_id != manifest.source_document_id
        or case.expected_document_version != manifest.source_document_version
        for case in cases
    ):
        raise RagDatasetError("RAG expected source identity is outside the manifest corpus")
    all_approved = all(case.review.status == "approved" for case in cases)
    manifest_approved = manifest.review_status == "approved" and manifest.human_review_complete
    if all_approved != manifest_approved:
        raise RagDatasetError("RAG review manifest and case status disagree")
    if require_human_review and not all_approved:
        raise HumanReviewRequired("RAG dataset is pending actual human review")
    return RagDataset(manifest=manifest, cases=tuple(cases))


def load_rag_eval_config(path: Path) -> RagEvalConfig:
    try:
        return RagEvalConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RagDatasetError("RAG evaluation configuration is unavailable or invalid") from exc


async def run_rag_eval(
    manifest_path: Path,
    *,
    config: ResolvedRuntimeConfig,
    reranker: Reranker,
    reranker_profile: str,
    split: Literal["development", "holdout", "all"] = "all",
    require_human_review: bool = True,
    gates_sha256: str | None = None,
    lock_sha256: str | None = None,
) -> RagEvalReport:
    dataset = load_rag_dataset(
        manifest_path,
        require_human_review=require_human_review,
    )
    selected = tuple(case for case in dataset.cases if split == "all" or case.split == split)
    if not selected:
        raise RagDatasetError("RAG evaluation selected no cases")
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="rag-eval",
            thread_id="rag-eval",
            run_id="rag-eval",
        )
        source = demo_rule_document()
        if (
            source.document_id != dataset.manifest.source_document_id
            or source.version != dataset.manifest.source_document_version
        ):
            raise RagDatasetError("RAG dataset source identity does not match the runtime fixture")
        await ctx.knowledge.ingest(source, ctx)
        if not isinstance(ctx.retriever, ConfigurableRetriever):
            raise RuntimeError("RAG evaluation requires the configurable retrieval pipeline")
        pipelines = {
            "dense": ctx.retriever.for_mode("dense"),
            "hybrid": ctx.retriever.for_mode("hybrid"),
            "hybrid_rerank": ctx.retriever.for_mode(
                "hybrid_rerank",
                reranker=reranker,
            ),
        }
        reports: list[RagPipelineReport] = []
        for mode in _PIPELINES:
            case_results = [await _evaluate_case(case, pipelines[mode], ctx) for case in selected]
            reports.append(
                RagPipelineReport(
                    mode=mode,
                    metrics=_metrics(case_results),
                    cases=tuple(case_results),
                )
            )
    finally:
        await runtime.aclose()
    identity = {
        "dataset_sha256": dataset.manifest.dataset_sha256,
        "runner_version": "rag_v1",
        "embedding_profile": config.embedding.model_dump(mode="json"),
        "reranker_profile": reranker_profile,
        "split": split,
        "pipelines": _PIPELINES,
        "gates_sha256": gates_sha256,
        "lock_sha256": lock_sha256,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RagEvalReport(
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.dataset_sha256,
        split=split,
        verification_level=("fixture" if config.embedding.provider == "fixture" else "community"),
        embedding_profile=config.embedding.profile_id,
        reranker_profile=reranker_profile,
        config_fingerprint=config.config_fingerprint,
        gates_sha256=gates_sha256,
        lock_sha256=lock_sha256,
        eval_fingerprint=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        executed_at=datetime.now().astimezone(),
        pipelines=tuple(reports),
    )


async def _evaluate_case(
    case: RagCase,
    retriever: ConfigurableRetriever,
    ctx: Context,
) -> RagCaseResult:
    started = time.perf_counter()
    docs = await retriever.retrieve(case.query, ctx, k=case.k)
    elapsed_ms = (time.perf_counter() - started) * 1000
    relevant_rank: int | None = None
    categories: list[str] = []
    valid_citation_count = 0
    for rank, doc in enumerate(docs, start=1):
        category = doc.metadata.get("rule_category")
        categories.append(category if isinstance(category, str) else "")
        document_id = doc.metadata.get("document_id")
        if (
            relevant_rank is None
            and document_id == case.expected_document_id
            and doc.version == case.expected_document_version
            and category == case.expected_rule_category
        ):
            relevant_rank = rank
        if await ctx.knowledge.citation_exists(
            CitationBlock(
                document_id=str(document_id),
                document_version=doc.version,
                chunk_id=doc.id,
            ),
            ctx,
        ):
            valid_citation_count += 1
    citations_valid = valid_citation_count == len(docs)
    return RagCaseResult(
        case_id=case.case_id,
        split=case.split,
        critical=case.critical,
        relevant_rank=relevant_rank,
        returned_chunk_ids=tuple(doc.id for doc in docs),
        returned_rule_categories=tuple(categories),
        valid_citation_count=valid_citation_count,
        citations_valid=citations_valid,
        latency_ms=elapsed_ms,
        passed=relevant_rank is not None and citations_valid,
    )


def _metrics(cases: list[RagCaseResult]) -> RagMetrics:
    count = len(cases)
    hits = sum(case.relevant_rank is not None for case in cases)
    recall = hits / count
    low, high = _wilson_interval(hits, count)
    reciprocal_ranks = [
        0.0 if case.relevant_rank is None else 1.0 / case.relevant_rank for case in cases
    ]
    returned = sum(len(case.returned_chunk_ids) for case in cases)
    citation_hits = sum(case.valid_citation_count for case in cases)
    critical = [case for case in cases if case.critical]
    if not critical:
        raise RagDatasetError("RAG evaluation selected no critical cases")
    latencies = sorted(case.latency_ms for case in cases)
    return RagMetrics(
        evaluated_cases=count,
        recall_at_k=recall,
        recall_ci95_low=low,
        recall_ci95_high=high,
        mrr=sum(reciprocal_ranks) / count,
        citation_hit_rate=citation_hits / returned if returned else 0.0,
        critical_pass_rate=sum(case.passed for case in critical) / len(critical),
        latency_p50_ms=median(latencies),
        latency_p95_ms=latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
    )


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def create_rag_baseline(report: RagEvalReport, *, created_at: datetime) -> RagBaseline:
    if (
        report.split != "all"
        or report.verification_level != "fixture"
        or report.embedding_profile != "fixture"
        or report.reranker_profile != "fixture"
        or report.gates_sha256 is None
        or report.lock_sha256 is None
    ):
        raise RagGoldenGateError("RAG baseline requires a fully bound Fixture all-split report")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise RagGoldenGateError("RAG baseline creation time must include a timezone")
    return RagBaseline(
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        gates_sha256=report.gates_sha256,
        lock_sha256=report.lock_sha256,
        eval_fingerprint=report.eval_fingerprint,
        created_at=created_at,
        pipelines=tuple(
            RagBaselinePipeline(
                mode=pipeline.mode,
                metrics=_quality_metrics(pipeline.metrics),
                cases=tuple(
                    RagBaselineCase(
                        case_id=case.case_id,
                        split=case.split,
                        critical=case.critical,
                        relevant_rank=case.relevant_rank,
                        returned_rule_categories=case.returned_rule_categories,
                        citations_valid=case.citations_valid,
                        passed=case.passed,
                    )
                    for case in pipeline.cases
                ),
            )
            for pipeline in report.pipelines
        ),
    )


def load_rag_baseline(path: Path) -> RagBaseline:
    try:
        return RagBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RagGoldenGateError("RAG baseline is unavailable or invalid") from exc


def load_rag_gates(path: Path) -> RagGates:
    try:
        return RagGates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RagGoldenGateError("RAG gates are unavailable or invalid") from exc


def assert_rag_gates(
    report: RagEvalReport,
    *,
    gates: RagGates,
    gates_sha256: str,
    lock_sha256: str,
    baseline: RagBaseline | None = None,
) -> None:
    if report.dataset_version != gates.dataset_version:
        raise RagGoldenGateError("RAG report and gates identify different datasets")
    if (
        report.split != "all"
        or report.verification_level != "fixture"
        or report.gates_sha256 != gates_sha256
        or report.lock_sha256 != lock_sha256
    ):
        raise RagGoldenGateError("RAG report is not bound to the selected deterministic gate")
    reports = {pipeline.mode: pipeline for pipeline in report.pipelines}
    if set(reports) != set(_PIPELINES):
        raise RagGoldenGateError("RAG report does not contain all three pipelines")
    for mode in _PIPELINES:
        observed = _quality_metrics(reports[mode].metrics)
        required = gates.required_metrics[mode]
        _assert_quality_not_lower(observed, required, prefix=f"required {mode}")
        if any(case.critical and not case.passed for case in reports[mode].cases):
            raise RagGoldenGateError(f"RAG critical case failed: {mode}")
    if baseline is None:
        return
    if (
        baseline.dataset_version != report.dataset_version
        or baseline.dataset_sha256 != report.dataset_sha256
        or baseline.runner_version != report.runner_version
        or baseline.gates_sha256 != gates_sha256
        or baseline.lock_sha256 != lock_sha256
        or baseline.eval_fingerprint != report.eval_fingerprint
    ):
        raise RagGoldenGateError("RAG report does not match the frozen baseline identity")
    baselines = {pipeline.mode: pipeline for pipeline in baseline.pipelines}
    if set(baselines) != set(_PIPELINES):
        raise RagGoldenGateError("RAG baseline does not contain all three pipelines")
    for mode in _PIPELINES:
        observed = _quality_metrics(reports[mode].metrics)
        _assert_quality_not_lower(observed, baselines[mode].metrics, prefix=f"baseline {mode}")
        current_cases = {case.case_id: case for case in reports[mode].cases}
        if set(current_cases) != {case.case_id for case in baselines[mode].cases}:
            raise RagGoldenGateError(f"RAG case identities changed from baseline: {mode}")
        for prior in baselines[mode].cases:
            current = current_cases[prior.case_id]
            if current.critical and (
                not current.passed
                or current.relevant_rank is None
                or prior.relevant_rank is None
                or current.relevant_rank > prior.relevant_rank
            ):
                raise RagGoldenGateError(
                    f"RAG critical case regressed from baseline: {mode}/{current.case_id}"
                )


def _quality_metrics(metrics: RagMetrics) -> RagQualityMetrics:
    return RagQualityMetrics(
        recall_at_k=metrics.recall_at_k,
        mrr=metrics.mrr,
        citation_hit_rate=metrics.citation_hit_rate,
        critical_pass_rate=metrics.critical_pass_rate,
    )


def _assert_quality_not_lower(
    observed: RagQualityMetrics,
    expected: RagQualityMetrics,
    *,
    prefix: str,
) -> None:
    comparisons = (
        ("recall_at_k", observed.recall_at_k, expected.recall_at_k),
        ("mrr", observed.mrr, expected.mrr),
        ("citation_hit_rate", observed.citation_hit_rate, expected.citation_hit_rate),
        ("critical_pass_rate", observed.critical_pass_rate, expected.critical_pass_rate),
    )
    for name, actual, minimum in comparisons:
        if actual < minimum:
            raise RagGoldenGateError(f"RAG {prefix} metric failed: {name}")
