"""Immutable knowledge and campaign-rule snapshot contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from oria.core.types import ACLMetadata, CitationBlock, JsonValue, ValueModel
from oria.domain.models import (
    BasicRule,
    BenefitRule,
    ConfirmationRule,
    EnrollmentRule,
    MerchantMaterialRule,
    RecruitmentScopeRule,
)

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
RuleCategory = Literal[
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
]


class DocumentIngestRequest(ValueModel):
    document_id: str = Field(pattern=_IDENTITY_PATTERN)
    version: str = Field(pattern=_IDENTITY_PATTERN)
    source_uri: str = Field(min_length=1, max_length=2048)
    owner_ref: str = Field(min_length=1, max_length=256)
    data_classification: Literal["public", "internal", "restricted"]
    content: str = Field(min_length=1, max_length=2_000_000, repr=False, exclude=True)
    acl: ACLMetadata = ACLMetadata()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class IndexedChunk(ValueModel):
    chunk_id: str = Field(pattern=r"^chk_[0-9a-f]{32}$")
    document_id: str
    document_version: str
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    public_content: str
    rule_category: RuleCategory | None = None


class IndexHit(ValueModel):
    chunk_id: str
    content: str
    metadata: dict[str, JsonValue]
    distance: float = Field(ge=0)


class BM25Hit(ValueModel):
    chunk_id: str
    content: str
    metadata: dict[str, JsonValue]
    score: float = Field(ge=0)


class IngestionResult(ValueModel):
    document_id: str
    document_version: str
    content_hash: str
    object_ref: str
    chunk_count: int = Field(ge=0)
    idempotent: bool


class RebuildResult(ValueModel):
    document_versions: int = Field(ge=0)
    chunk_count: int = Field(ge=0)


class DeletionResult(ValueModel):
    document_id: str
    deleted_versions: int = Field(ge=0)


class CatalogVersion(ValueModel):
    tenant_id: str
    document_id: str
    version: str
    source_uri: str
    owner_ref: str
    data_classification: str
    content_hash: str
    object_ref: str
    acl: ACLMetadata
    metadata: dict[str, JsonValue]
    chunking_version: str
    embedding_profile: str


class FieldEvidence(ValueModel):
    source_document_id: str
    source_version: str
    chunk_id: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    validation_status: Literal["valid"] = "valid"

    def as_citation(self) -> CitationBlock:
        return CitationBlock(
            document_id=self.source_document_id,
            document_version=self.source_version,
            chunk_id=self.chunk_id,
        )


class CampaignRuleSnapshot(ValueModel):
    snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, repr=False, exclude=True)
    effective_at: datetime
    basic: BasicRule
    recruitment_scope: RecruitmentScopeRule
    enrollment_policy: EnrollmentRule
    benefit_policy: BenefitRule
    confirmation_policy: ConfirmationRule
    merchant_material: MerchantMaterialRule
    field_evidence: dict[str, FieldEvidence]

    @field_validator("effective_at")
    @classmethod
    def require_aware_effective_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_at must include a timezone")
        return value

    def categories(self) -> tuple[RuleCategory, ...]:
        return (
            "basic",
            "recruitment_scope",
            "enrollment_policy",
            "benefit_policy",
            "confirmation_policy",
            "merchant_material",
        )

    def recompute_hash(self) -> str:
        return snapshot_hash(self.internal_payload())

    def internal_payload(self) -> dict[str, JsonValue]:
        scope = self.recruitment_scope
        return {
            "tenant_id": self.tenant_id,
            "effective_at": self.effective_at.isoformat(),
            "basic": self.basic.model_dump(mode="json"),
            "recruitment_scope": {
                **scope.model_dump(mode="json"),
                "allowlist_merchant_ids": list(sorted(scope.internal_allowlist())),
                "denylist_merchant_ids": list(sorted(scope.internal_denylist())),
                "sales_org_scope": list(sorted(scope.internal_sales_org_scope())),
            },
            "enrollment_policy": self.enrollment_policy.model_dump(mode="json"),
            "benefit_policy": self.benefit_policy.model_dump(mode="json"),
            "confirmation_policy": self.confirmation_policy.model_dump(mode="json"),
            "merchant_material": self.merchant_material.model_dump(mode="json"),
            "field_evidence": {
                path: evidence.model_dump(mode="json")
                for path, evidence in sorted(self.field_evidence.items())
            },
        }


class RuleSnapshotResolution(ValueModel):
    snapshot: CampaignRuleSnapshot | None = None
    unresolved_items: tuple[str, ...] = ()


def snapshot_hash(payload: dict[str, JsonValue]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
