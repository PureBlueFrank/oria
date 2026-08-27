"""Deterministic campaign rule resolution and tenant-qualified snapshot cache."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from oria.core.types import CitationBlock, Doc, JsonValue
from oria.domain.models import (
    BasicRule,
    BenefitRule,
    ConfirmationRule,
    EnrollmentRule,
    MerchantMaterialRule,
    RecruitmentScopeRule,
)
from oria.rag.catalog import SQLiteKnowledgeCatalog
from oria.rag.errors import KnowledgeError, RuleSnapshotError
from oria.rag.models import (
    CampaignRuleSnapshot,
    FieldEvidence,
    RuleCategory,
    RuleSnapshotResolution,
)
from oria.rag.service import LocalKnowledgeService

if TYPE_CHECKING:
    from oria.core.context import Context

_CATEGORIES: tuple[RuleCategory, ...] = (
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
)
_MODELS: dict[RuleCategory, type[Any]] = {
    "basic": BasicRule,
    "recruitment_scope": RecruitmentScopeRule,
    "enrollment_policy": EnrollmentRule,
    "benefit_policy": BenefitRule,
    "confirmation_policy": ConfirmationRule,
    "merchant_material": MerchantMaterialRule,
}


class LocalRuleSnapshotStore:
    def __init__(self, catalog: SQLiteKnowledgeCatalog, knowledge: LocalKnowledgeService) -> None:
        self._catalog = catalog
        self._knowledge = knowledge

    async def resolve(
        self,
        docs: list[Doc],
        *,
        effective_at: datetime,
        ctx: Context,
    ) -> RuleSnapshotResolution:
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("effective_at must include a timezone")
        selected: dict[RuleCategory, tuple[dict[str, JsonValue], FieldEvidence]] = {}
        unresolved: list[str] = []
        for category in _CATEGORIES:
            candidates: list[tuple[int, str, dict[str, JsonValue], FieldEvidence]] = []
            for doc in docs:
                if doc.tenant_id != ctx.tenant_id or doc.metadata.get("rule_category") != category:
                    continue
                if not _is_effective(doc, effective_at):
                    continue
                citation = CitationBlock(
                    document_id=str(doc.metadata.get("document_id", "")),
                    document_version=doc.version,
                    chunk_id=doc.id,
                )
                try:
                    observed_category, section = await self._knowledge.load_rule_section(
                        citation, ctx
                    )
                except (KnowledgeError, ValueError):
                    continue
                if observed_category != category:
                    continue
                priority = doc.metadata.get("priority")
                if not isinstance(priority, int) or isinstance(priority, bool):
                    continue
                canonical = json.dumps(
                    section, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                candidates.append(
                    (
                        priority,
                        canonical,
                        section,
                        FieldEvidence(
                            source_document_id=citation.document_id,
                            source_version=citation.document_version,
                            chunk_id=citation.chunk_id,
                        ),
                    )
                )
            if not candidates:
                unresolved.append(f"missing:{category}")
                continue
            top_priority = max(item[0] for item in candidates)
            top = [item for item in candidates if item[0] == top_priority]
            if len({item[1] for item in top}) != 1:
                unresolved.append(f"conflict:{category}")
                continue
            chosen = sorted(top, key=lambda item: item[3].chunk_id)[0]
            selected[category] = (chosen[2], chosen[3])
        if unresolved:
            return RuleSnapshotResolution(unresolved_items=tuple(sorted(unresolved)))

        try:
            values = {
                category: _MODELS[category].model_validate(selected[category][0])
                for category in _CATEGORIES
            }
        except (ValidationError, ValueError) as exc:
            return RuleSnapshotResolution(unresolved_items=(f"invalid_rule:{type(exc).__name__}",))
        evidence: dict[str, FieldEvidence] = {}
        for category in _CATEGORIES:
            section, source = selected[category]
            for path in _leaf_paths(section, prefix=category):
                evidence[path] = source

        placeholder = CampaignRuleSnapshot(
            snapshot_id="rs_" + "a" * 24,
            snapshot_hash="sha256:" + "0" * 64,
            tenant_id=ctx.tenant_id,
            effective_at=effective_at,
            basic=cast(BasicRule, values["basic"]),
            recruitment_scope=cast(RecruitmentScopeRule, values["recruitment_scope"]),
            enrollment_policy=cast(EnrollmentRule, values["enrollment_policy"]),
            benefit_policy=cast(BenefitRule, values["benefit_policy"]),
            confirmation_policy=cast(ConfirmationRule, values["confirmation_policy"]),
            merchant_material=cast(MerchantMaterialRule, values["merchant_material"]),
            field_evidence=evidence,
        )
        digest = placeholder.recompute_hash()
        existing = await self._catalog.get_snapshot_row(ctx.tenant_id, snapshot_hash=digest)
        if existing is not None:
            return RuleSnapshotResolution(snapshot=await self.get(existing[0], ctx))
        snapshot = placeholder.model_copy(
            update={"snapshot_id": f"rs_{secrets.token_urlsafe(24)}", "snapshot_hash": digest}
        )
        payload_json = json.dumps(
            snapshot.internal_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._catalog.insert_snapshot(
            ctx.tenant_id,
            snapshot.snapshot_id,
            snapshot.snapshot_hash,
            payload_json,
        )
        persisted = await self._catalog.get_snapshot_row(ctx.tenant_id, snapshot_hash=digest)
        if persisted is None:
            raise RuleSnapshotError("rule snapshot cache write failed")
        return RuleSnapshotResolution(snapshot=await self.get(persisted[0], ctx))

    async def get(self, snapshot_id: str, ctx: Context) -> CampaignRuleSnapshot:
        row = await self._catalog.get_snapshot_row(ctx.tenant_id, snapshot_id=snapshot_id)
        if row is None:
            raise RuleSnapshotError("rule snapshot is unavailable")
        stored_id, stored_hash, payload_json = row
        try:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError("snapshot payload must be an object")
            snapshot = CampaignRuleSnapshot.model_validate(
                {**payload, "snapshot_id": stored_id, "snapshot_hash": stored_hash}
            )
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise RuleSnapshotError("rule snapshot integrity verification failed") from exc
        if snapshot.recompute_hash() != stored_hash:
            raise RuleSnapshotError("rule snapshot integrity verification failed")
        for evidence in snapshot.field_evidence.values():
            if not await self._knowledge.citation_exists(evidence.as_citation(), ctx):
                raise RuleSnapshotError("rule snapshot contains a stale citation")
        return snapshot


def _is_effective(doc: Doc, effective_at: datetime) -> bool:
    start = _metadata_datetime(doc.metadata.get("effective_from"))
    end = _metadata_datetime(doc.metadata.get("effective_to"))
    return start is not None and end is not None and start <= effective_at <= end


def _metadata_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _leaf_paths(value: object, *, prefix: str) -> tuple[str, ...]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in sorted(value.items()):
            paths.extend(_leaf_paths(child, prefix=f"{prefix}.{key}"))
        return tuple(paths)
    if isinstance(value, (list, tuple)):
        if not value:
            return (prefix,)
        paths = []
        for index, child in enumerate(value):
            paths.extend(_leaf_paths(child, prefix=f"{prefix}.{index}"))
        return tuple(paths)
    return (prefix,)
