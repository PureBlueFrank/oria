"""Strict loader and human-review gate for versioned Golden datasets."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from oria.core.types import JsonValue, ValueModel

_DATASET_FILE = re.compile(r"^v[1-9][0-9]*\.jsonl$")


class GoldenDatasetError(ValueError):
    """Raised when a dataset or manifest violates its frozen contract."""


class HumanReviewRequired(GoldenDatasetError):
    """Raised when an automated caller attempts to use unreviewed cases."""


class GoldenReview(ValueModel):
    status: Literal["pending_human_review", "approved"]
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def require_reviewer_for_approval(self) -> Self:
        if self.status == "approved":
            if not self.reviewed_by or self.reviewed_at is None:
                raise ValueError("approved Golden cases require reviewer identity and time")
            if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
                raise ValueError("Golden review time must include a timezone")
        elif self.reviewed_by is not None or self.reviewed_at is not None:
            raise ValueError("pending Golden cases cannot claim reviewer metadata")
        return self


class GoldenCase(ValueModel):
    case_id: str = Field(pattern=r"^sa-v[1-9][0-9]*-[0-9]{3}$")
    schema_version: Literal[1] = 1
    critical: bool
    input: str = Field(min_length=1)
    fixture_variant: str = Field(min_length=1)
    expected_outcome: Literal["proposal", "abstain", "runtime_failure"]
    expected_rule_fields: tuple[str, ...] = ()
    expected_hard_eligible_ids: tuple[str, ...] = ()
    expected_excluded_ids: tuple[str, ...] = ()
    expected_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...] = ()
    expected_unresolved_items: tuple[str, ...] = ()
    expected_reason: str | None = None
    output_mutation: dict[str, JsonValue] | None = None
    review: GoldenReview

    @model_validator(mode="after")
    def validate_expected_shape(self) -> Self:
        if self.expected_outcome == "proposal" and not self.expected_hard_eligible_ids:
            raise ValueError("proposal cases require the expected hard-eligible set")
        if self.expected_outcome == "abstain" and not self.expected_unresolved_items:
            raise ValueError("abstain cases require unresolved items")
        if self.expected_outcome == "runtime_failure" and not self.expected_reason:
            raise ValueError("runtime failures require an expected reason")
        if set(self.expected_hard_eligible_ids) & set(self.expected_excluded_ids):
            raise ValueError("eligible and excluded merchant sets must be disjoint")
        return self


class AttributionGoldenCase(ValueModel):
    """Scenario B golden case for attribution eval with human-review gate."""

    case_id: str = Field(pattern=r"^sb-v[1-9][0-9]*-[0-9]{3}$")
    schema_version: Literal[1] = 1
    critical: bool
    tenant_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    fixture_variant: str = Field(min_length=1)
    expected_outcome: Literal["attributed", "conflicting", "insufficient"]
    expected_abstain: bool
    root_cause_code: str | None = None
    acceptable_hypotheses: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    golden_rationale: str = Field(min_length=1)
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    review: GoldenReview

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        if self.expected_outcome == "insufficient":
            if not self.expected_abstain:
                raise ValueError("insufficient cases must expect abstention")
            if self.root_cause_code is not None:
                raise ValueError("insufficient cases cannot have a root cause code")
        else:
            if self.expected_abstain:
                raise ValueError("attributed/conflicting cases cannot expect abstention")
            if not self.acceptable_hypotheses or not self.required_evidence:
                raise ValueError("attributed/conflicting cases require hypotheses and evidence")
            if not self.expected_tools:
                raise ValueError("attributed/conflicting cases require expected tools")
            if self.expected_outcome == "conflicting" and len(self.acceptable_hypotheses) < 2:
                raise ValueError("conflicting cases require multiple acceptable hypotheses")
        return self


GoldenCaseModel = GoldenCase | AttributionGoldenCase
"""Union of all golden case models; dispatch is driven by ``GoldenManifest.suite``."""


class GoldenManifest(ValueModel):
    suite: Literal["scenario_a", "scenario_b"]
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    schema_version: Literal[1] = 1
    source: Literal["synthetic"]
    contains_real_entities: Literal[False]
    license: str = Field(min_length=1)
    generator_seed: str = Field(min_length=1)
    case_count: int = Field(ge=30)
    critical_case_count: int = Field(ge=1)
    dataset_file: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_status: Literal["pending_human_review", "approved"]
    human_review_complete: bool
    baseline_created: bool

    @model_validator(mode="after")
    def validate_review_and_baseline_status(self) -> Self:
        approved = self.review_status == "approved"
        if self.human_review_complete != approved:
            raise ValueError("Golden manifest review fields disagree")
        if self.baseline_created and not approved:
            raise ValueError("Golden baseline cannot precede actual human review")
        return self


class GoldenDataset(ValueModel):
    manifest: GoldenManifest
    cases: tuple[GoldenCaseModel, ...]

    @model_validator(mode="after")
    def validate_suite_case_type(self) -> Self:
        for case in self.cases:
            if self.manifest.suite == "scenario_a" and not isinstance(case, GoldenCase):
                raise GoldenDatasetError("scenario_a cases must use GoldenCase schema")
            if self.manifest.suite == "scenario_b" and not isinstance(case, AttributionGoldenCase):
                raise GoldenDatasetError("scenario_b cases must use AttributionGoldenCase schema")
        return self


_CASE_MODELS: dict[str, type[GoldenCaseModel]] = {
    "scenario_a": GoldenCase,
    "scenario_b": AttributionGoldenCase,
}


def load_golden_dataset(
    manifest_path: Path,
    *,
    require_human_review: bool = True,
) -> GoldenDataset:
    """Load, integrity-check, and optionally enforce actual human approval."""

    try:
        manifest = GoldenManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenDatasetError("Golden manifest is unavailable or invalid") from exc
    if _DATASET_FILE.fullmatch(manifest.dataset_file) is None:
        raise GoldenDatasetError("Golden dataset filename is invalid")
    dataset_path = manifest_path.parent / manifest.dataset_file
    try:
        payload = dataset_path.read_bytes()
    except OSError as exc:
        raise GoldenDatasetError("Golden dataset is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != manifest.dataset_sha256:
        raise GoldenDatasetError("Golden dataset integrity check failed")
    case_model = _CASE_MODELS[manifest.suite]
    cases: list[GoldenCaseModel] = []
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                raise GoldenDatasetError("Golden dataset contains a blank line")
            cases.append(case_model.model_validate_json(line))
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, GoldenDatasetError):
            raise
        raise GoldenDatasetError("Golden dataset contains an invalid case") from exc
    if len(cases) != manifest.case_count or len({case.case_id for case in cases}) != len(cases):
        raise GoldenDatasetError("Golden case count or identity is invalid")
    if sum(case.critical for case in cases) != manifest.critical_case_count:
        raise GoldenDatasetError("Golden critical-case count is invalid")
    all_approved = all(case.review.status == "approved" for case in cases)
    manifest_approved = manifest.review_status == "approved" and manifest.human_review_complete
    if all_approved != manifest_approved:
        raise GoldenDatasetError("Golden review manifest and case status disagree")
    if require_human_review and not all_approved:
        raise HumanReviewRequired("Golden dataset is pending actual human review")
    return GoldenDataset(manifest=manifest, cases=tuple(cases))
