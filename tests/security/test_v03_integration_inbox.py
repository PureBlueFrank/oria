"""V0.3-T02 inbox duplicate, ordering, authorization, and redaction tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from oria.core.integration_events import (
    ExternalWait,
    IntegrationEventInboxService,
    IntegrationInboxRecord,
)

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


class _Repository:
    def __init__(self) -> None:
        self.records: list[IntegrationInboxRecord] = []

    async def add(self, record: IntegrationInboxRecord) -> bool:
        key = (record.tenant_id, record.adapter_id, record.source_event_id)
        if any(
            (item.tenant_id, item.adapter_id, item.source_event_id) == key for item in self.records
        ):
            return False
        self.records.append(record)
        return True


def _event(
    *,
    source_event_id: str = "source-event-1",
    event_type: str = "merchant.enrollment_upserted",
    version: int = 2,
    signature_subject: str = "adapter-principal-a",
    campaign_id: str = "campaign-1",
) -> dict[str, object]:
    payloads: dict[str, dict[str, object]] = {
        "merchant.enrollment_upserted": {
            "campaign_id": campaign_id,
            "enrollment_id": "enrollment-1",
            "merchant_id": "synthetic-sensitive-merchant",
            "product_ref": "product-1",
            "product_version": "v1",
        },
        "enrollment.window_closed": {
            "campaign_id": campaign_id,
            "enrollment_window_ref": "window-v1",
        },
    }
    return {
        "schema_version": 1,
        "event_type": event_type,
        "tenant_id": "tenant-a",
        "adapter_id": "adapter-a",
        "source_event_id": source_event_id,
        "signature_subject": signature_subject,
        "version": version,
        "payload": payloads.get(event_type, {}),
    }


def _wait(**updates: object) -> ExternalWait:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "wait_id": "wait-1",
        "event_type": "merchant.enrollment_upserted",
        "resource_type": "campaign",
        "resource_id": "campaign-1",
        "expected_version": 2,
        "checkpoint_id": "checkpoint-trusted-1",
        "expires_at": NOW + timedelta(hours=1),
        "timeout_action": "fail",
        "status": "waiting",
        "created_at": NOW,
    }
    values.update(updates)
    return ExternalWait.model_validate(values)


def _service(repository: _Repository) -> IntegrationEventInboxService:
    return IntegrationEventInboxService(
        repository,
        authorized_subjects={("tenant-a", "adapter-a"): frozenset({"adapter-principal-a"})},
        clock=lambda: NOW + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_matching_event_is_sanitized_hashed_and_duplicate_never_resumes() -> None:
    repository = _Repository()
    service = _service(repository)

    first = await service.process(_event(), wait=_wait())
    duplicate = await service.process(_event(), wait=_wait())

    assert first.status == "matched" and first.resume_eligible is True
    assert duplicate.status == "duplicate" and duplicate.resume_eligible is False
    assert len(repository.records) == 1
    record = repository.records[0]
    assert record.payload_hash.startswith("sha256:")
    assert record.redacted_payload["merchant_id"] == "[REDACTED]"
    serialized = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    assert "synthetic-sensitive-merchant" not in serialized
    assert "checkpoint-trusted-1" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_updates", "wait_updates", "expected"),
    [
        ({"signature_subject": "attacker"}, {}, "unauthorized"),
        ({}, {"event_type": "enrollment.window_closed"}, "type_mismatch"),
        ({"campaign_id": "campaign-2"}, {}, "resource_mismatch"),
        ({"version": 1}, {}, "stale"),
        ({"version": 3}, {}, "out_of_order"),
        ({}, {"expires_at": NOW + timedelta(seconds=30)}, "wait_expired"),
    ],
)
async def test_untrusted_mismatched_stale_and_out_of_order_events_are_not_resume_eligible(
    event_updates: dict[str, object],
    wait_updates: dict[str, object],
    expected: str,
) -> None:
    repository = _Repository()
    event = _event(**event_updates)  # type: ignore[arg-type]

    result = await _service(repository).process(event, wait=_wait(**wait_updates))

    assert result.status == expected
    assert result.resume_eligible is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"event_type": "selection.unknown"},
        {"schema_version": 2},
        {"raw_body": "synthetic-raw-secret"},
    ],
)
async def test_unknown_wrong_schema_and_raw_body_are_rejected_before_persistence(
    mutation: dict[str, object],
) -> None:
    repository = _Repository()
    event = _event()
    event.update(mutation)

    result = await _service(repository).process(event, wait=_wait())

    assert result.status == "invalid_envelope"
    assert result.resume_eligible is False
    assert repository.records == []
