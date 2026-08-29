"""Contracts for immutable, finite and safely projected core value types."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from oria.core.types import (
    ACLFilter,
    ACLMetadata,
    AuthorizationContext,
    ChatResult,
    ContentBlock,
    EventEnvelope,
    Message,
    PolicyDecision,
    ProviderCapabilities,
    ProviderExtensionBlock,
    QueryFilters,
    ReasoningDelta,
    ResourceRef,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCall,
    Usage,
)

pytestmark = pytest.mark.contract


def test_cross_seam_containers_are_deeply_immutable_and_detached_from_inputs() -> None:
    args: dict[str, Any] = {"tenant": "safe", "nested": {"ids": [1, 2]}}
    call = ToolCall(id="call-1", name="query", args=args)
    auth = AuthorizationContext(correlation_id="corr-1", attributes={"scope": "read"})
    message = Message(role="assistant", content=[TextBlock(text="safe")])

    with pytest.raises(TypeError):
        call.args["tenant"] = "HIJACKED"
    mapping_view: Any = call.args
    with pytest.raises(TypeError):
        mapping_view |= {"tenant": "HIJACKED"}
    nested: Any = call.args["nested"]
    with pytest.raises(TypeError):
        nested["ids"] += (3,)
    list_view: Any = nested["ids"]
    with pytest.raises(TypeError):
        list_view += [3]
    with pytest.raises(TypeError):
        auth.attributes["injected"] = "x"
    content: Any = message.content
    with pytest.raises(AttributeError):
        content.append(TextBlock(text="tampered"))

    args["tenant"] = "changed-after-validation"
    args["nested"]["ids"].append(3)
    assert call.model_dump(mode="json")["args"] == {
        "tenant": "safe",
        "nested": {"ids": [1, 2]},
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_values_reject_non_finite_floats(value: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        ToolCall(id="call-1", name="query", args={"value": value})


def test_finite_json_float_round_trips() -> None:
    value = 12.375
    call = ToolCall(id="call-1", name="query", args={"value": value})

    restored = ToolCall.model_validate_json(call.model_dump_json())

    assert restored.args["value"] == value
    assert math.isfinite(restored.args["value"])


def test_sensitive_provider_fields_use_public_projection_by_default() -> None:
    reasoning_secret = "internal-chain-of-thought"
    reasoning = ReasoningDelta(
        sequence=1,
        provider="mock",
        model="mock-demo",
        text=reasoning_secret,
    )
    raw_secret = "provider-raw-secret"
    result = ChatResult(
        content=[TextBlock(text="visible")],
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        raw_response={"secret": raw_secret},
    )

    assert reasoning.internal_text() == reasoning_secret
    assert result.internal_raw_response() == {"secret": raw_secret}
    assert reasoning_secret not in repr(reasoning)
    assert reasoning_secret not in reasoning.model_dump_json()
    assert "text" not in reasoning.model_dump()
    assert raw_secret not in repr(result)
    assert raw_secret not in result.model_dump_json()
    assert "raw_response" not in result.model_dump()


@pytest.mark.parametrize(
    ("structured_output", "modes"),
    [(True, frozenset()), (False, frozenset({"native_json_schema"}))],
)
def test_provider_capabilities_reject_inconsistent_structured_output(
    structured_output: bool,
    modes: frozenset[str],
) -> None:
    with pytest.raises(ValidationError, match="structured_output"):
        ProviderCapabilities(
            tool_calling=True,
            streaming=True,
            reasoning=False,
            structured_output=structured_output,
            parallel_tool_calls=True,
            structured_output_modes=modes,
            api_dialect="responses",
        )


def test_content_block_and_stream_event_discriminated_unions_round_trip() -> None:
    content_adapter = TypeAdapter(ContentBlock)
    stream_adapter = TypeAdapter(StreamEvent)
    block = ProviderExtensionBlock(raw_type="vendor", raw_payload={"items": [1, 2]})
    event = TextDelta(sequence=2, provider="mock", model="mock-demo", text="hello")

    restored_block = content_adapter.validate_json(content_adapter.dump_json(block))
    restored_event = stream_adapter.validate_json(stream_adapter.dump_json(event))

    assert restored_block == block
    assert restored_event == event


def test_acl_filter_is_deny_by_default_and_policy_constraints_cannot_be_overridden() -> None:
    default_filter = ACLFilter(tenant_id="tenant-a")
    public_acl = ACLMetadata()

    assert not default_filter.allows(
        tenant_id="tenant-a", acl=public_acl, classification="internal"
    )

    acl_filter = ACLFilter(
        tenant_id="tenant-a",
        allowed_subject_ids=("subject-a",),
        allowed_roles=("reader",),
        classifications=("public", "internal"),
    )
    combined = acl_filter.and_query_filters(QueryFilters(attributes={"document_id": "document-a"}))

    assert combined.attributes["tenant_id"] == "tenant-a"
    assert combined.attributes["document_id"] == "document-a"
    with pytest.raises(TypeError):
        combined.attributes["tenant_id"] = "tenant-b"
    with pytest.raises(ValueError, match="reserved"):
        acl_filter.and_query_filters(QueryFilters(attributes={"tenant_id": "tenant-b"}))

    decision = PolicyDecision(
        allow=True,
        constraints={"tenant_id": "tenant-a"},
        policy_version="policy-v1",
        reason="allowed",
        acl_filter=acl_filter,
    )
    assert decision.require_acl_filter() == acl_filter
    decision_values = decision.model_dump()
    decision_values["constraints"] = {"tenant_id": "tenant-b"}
    with pytest.raises(ValidationError, match="tenant"):
        PolicyDecision.model_validate(decision_values)


def test_event_envelope_requires_aware_time_and_round_trips() -> None:
    event = EventEnvelope(
        event_id="audit-1",
        occurred_at=datetime(2026, 8, 29, tzinfo=UTC),
        tenant_id="tenant-a",
        actor="subject-a",
        action="document:read",
        resource=ResourceRef(
            resource_type="document", resource_id="document-a", tenant_id="tenant-a"
        ),
        decision="deny",
        policy_version="policy-v1",
        args_hash="sha256:" + "0" * 64,
        result="denied",
        correlation_id="correlation-a",
        payload={"reason_code": "acl_denied"},
    )

    assert EventEnvelope.model_validate_json(event.model_dump_json()) == event
    values = event.model_dump()
    values["occurred_at"] = datetime(2026, 8, 29)
    with pytest.raises(ValidationError, match="timezone"):
        EventEnvelope.model_validate(values)
