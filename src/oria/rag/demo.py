"""Build the synthetic T03 campaign rules as a versioned T05 source document."""

from __future__ import annotations

import json

from oria.core.types import ACLMetadata, JsonValue
from oria.rag.models import DocumentIngestRequest
from oria.resources.loader import load_demo_data


def demo_rule_document() -> DocumentIngestRequest:
    rules = load_demo_data().rules
    scope = rules.recruitment_scope
    enrollment_policy = rules.enrollment_policy.model_dump(mode="json")
    if rules.enrollment_policy.late_event_action == "reject":
        # The v1 synthetic source predates this explicit field and therefore uses the
        # fail-closed default. Keep its retrieval projection byte-stable; the resolved
        # immutable snapshot still includes and hashes the normalized default.
        enrollment_policy.pop("late_event_action")
    payload: dict[str, JsonValue] = {
        "basic": rules.basic.model_dump(mode="json"),
        "recruitment_scope": {
            **scope.model_dump(mode="json"),
            "allowlist_merchant_ids": list(scope.allowlist_merchant_ids),
            "denylist_merchant_ids": list(scope.denylist_merchant_ids),
            "sales_org_scope": list(scope.sales_org_scope),
        },
        "enrollment_policy": enrollment_policy,
        "benefit_policy": rules.benefit_policy.model_dump(mode="json"),
        "confirmation_policy": rules.confirmation_policy.model_dump(mode="json"),
        "merchant_material": rules.merchant_material.model_dump(mode="json"),
    }
    return DocumentIngestRequest(
        document_id="demo-campaign-rules",
        version=rules.version,
        source_uri="package://oria.resources.demo_data/campaign_rules.v1.json",
        owner_ref="oria-synthetic-fixture",
        data_classification="restricted",
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        acl=ACLMetadata(allowed_roles=("operator",), classification="restricted"),
        metadata={
            "document_kind": "campaign_rules",
            "rule_type": "merchant_recruitment",
            "effective_from": "2026-07-01T00:00:00+08:00",
            "effective_to": "2026-08-31T23:59:59+08:00",
            "priority": 100,
            "supersedes": "none",
            "template_ref": rules.basic.template_ref,
        },
    )
