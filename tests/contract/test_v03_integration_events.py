"""V0.3-T02 discriminated integration event union contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oria.core.integration_events import (
    EnrollmentWindowClosed,
    MerchantEnrollmentUpserted,
    SelectionCompleted,
    SelectionDecisionRecorded,
    parse_integration_event,
)

pytestmark = pytest.mark.contract


def _event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": event_type,
        "tenant_id": "tenant-a",
        "adapter_id": "adapter-a",
        "source_event_id": f"event-{event_type}",
        "signature_subject": "integration-principal-a",
        "version": 2,
        "payload": payload,
    }


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_type"),
    [
        (
            "merchant.enrollment_upserted",
            {
                "campaign_id": "campaign-1",
                "enrollment_id": "enrollment-1",
                "merchant_id": "merchant-1",
                "product_ref": "product-1",
                "product_version": "v1",
            },
            MerchantEnrollmentUpserted,
        ),
        (
            "enrollment.window_closed",
            {"campaign_id": "campaign-1", "enrollment_window_ref": "window-v1"},
            EnrollmentWindowClosed,
        ),
        (
            "selection.decision_recorded",
            {
                "campaign_id": "campaign-1",
                "submission_version": "submission-v1",
                "selection_version": "selection-v1",
                "enrollment_item_id": "item-1",
                "decision": "selected",
            },
            SelectionDecisionRecorded,
        ),
        (
            "selection.completed",
            {
                "campaign_id": "campaign-1",
                "submission_version": "submission-v1",
                "selection_version": "selection-v1",
            },
            SelectionCompleted,
        ),
    ],
)
def test_event_union_accepts_only_the_four_declared_shapes(
    event_type: str,
    payload: dict[str, object],
    expected_type: type[object],
) -> None:
    parsed = parse_integration_event(_event(event_type, payload))

    assert isinstance(parsed, expected_type)
    assert parsed.version == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"event_type": "selection.unknown"},
        {"schema_version": 2},
        {"version": 0},
        {"signature_subject": ""},
        {"checkpoint_id": "caller-controlled"},
    ],
)
def test_event_union_rejects_unknown_version_untrusted_and_extra_fields(
    mutation: dict[str, object],
) -> None:
    value = _event(
        "selection.completed",
        {
            "campaign_id": "campaign-1",
            "submission_version": "submission-v1",
            "selection_version": "selection-v1",
        },
    )
    value.update(mutation)

    with pytest.raises(ValidationError):
        parse_integration_event(value)
