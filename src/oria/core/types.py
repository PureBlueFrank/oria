"""Provider-neutral, serializable value types used by Oria core contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)


class ValueModel(BaseModel):
    """Strict immutable base for values crossing Oria component seams."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TextBlock(ValueModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(ValueModel):
    type: Literal["image"] = "image"
    source: str
    media_type: str | None = None


class ToolCallBlock(ValueModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, JsonValue]


class ToolResultBlock(ValueModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str


class CitationBlock(ValueModel):
    type: Literal["citation"] = "citation"
    document_id: str
    document_version: str
    chunk_id: str
    text: str | None = None


class RefusalBlock(ValueModel):
    type: Literal["refusal"] = "refusal"
    reason: str


class ProviderExtensionBlock(ValueModel):
    type: Literal["provider_extension"] = "provider_extension"
    raw_type: str
    raw_payload: dict[str, JsonValue]


ContentBlock: TypeAlias = Annotated[
    TextBlock
    | ImageBlock
    | ToolCallBlock
    | ToolResultBlock
    | CitationBlock
    | RefusalBlock
    | ProviderExtensionBlock,
    Field(discriminator="type"),
]


class Message(ValueModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]
    tool_call_id: str | None = None


class ToolCall(ValueModel):
    id: str
    name: str
    args: dict[str, JsonValue]


class ToolSpec(ValueModel):
    name: str
    schema_version: int = Field(ge=1)
    description: str
    json_schema: dict[str, JsonValue]
    strict: bool = True


class ResponseSchema(ValueModel):
    name: str
    json_schema: dict[str, JsonValue]
    strict: bool = True


class ChatOptions(ValueModel):
    temperature: float | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    tool_choice: str | dict[str, JsonValue] | None = None
    parallel_tool_calls: bool | None = None
    response_schema: ResponseSchema | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)


class Usage(ValueModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)


class ProviderCapabilities(ValueModel):
    tool_calling: bool
    streaming: bool
    reasoning: bool
    structured_output: bool
    parallel_tool_calls: bool
    structured_output_modes: frozenset[Literal["native_json_schema", "synthetic_tool"]]
    api_dialect: Literal["mock", "chat_completions", "responses", "anthropic_messages"]
    multimodal_inputs: frozenset[str] = frozenset()
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)


class ChatResult(ValueModel):
    content: list[ContentBlock]
    tool_calls: list[ToolCall]
    structured_output: dict[str, JsonValue] | None = None
    usage: Usage
    finish_reason: str | None = None
    request_id: str | None = None
    refusal: str | None = None
    raw_response: dict[str, JsonValue] | None = Field(default=None, repr=False)


class StreamEventBase(ValueModel):
    sequence: int = Field(ge=0)
    provider: str
    model: str
    request_id: str | None = None


class TextDelta(StreamEventBase):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallDelta(StreamEventBase):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    tool_call_id: str
    arguments_delta: str


class ReasoningDelta(StreamEventBase):
    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str = Field(repr=False)


class UsageDelta(StreamEventBase):
    type: Literal["usage_delta"] = "usage_delta"
    usage: Usage


class Done(StreamEventBase):
    type: Literal["done"] = "done"
    finish_reason: str | None = None


class ProviderError(StreamEventBase):
    type: Literal["provider_error"] = "provider_error"
    code: str
    safe_message: str
    retryable: bool


StreamEvent: TypeAlias = Annotated[
    TextDelta | ToolCallDelta | ReasoningDelta | UsageDelta | Done | ProviderError,
    Field(discriminator="type"),
]


class RetryPolicy(ValueModel):
    max_attempts: int = Field(default=1, ge=1)
    initial_backoff_seconds: float = Field(default=0.1, ge=0)
    max_backoff_seconds: float = Field(default=1.0, ge=0)


class ToolPolicy(ValueModel):
    risk_level: Literal["low", "medium", "high"]
    side_effect: bool
    timeout_seconds: float = Field(gt=0)
    retry_policy: RetryPolicy
    idempotency_scope: str | None = None
    required_action: str
    resource_type: str
    redact_fields: tuple[str, ...] = ()
    approval_mode: Literal["none", "conditional", "required"]
    approval_action: str | None = None
    business_confirmation: bool = False


class ToolError(ValueModel):
    code: str
    safe_message: str
    retryable: bool
    details_ref: str | None = None


class ToolResult(ValueModel):
    ok: bool
    data: JsonValue = None
    error: ToolError | None = None
    execution_id: str
    idempotency_key: str | None = None
    trust_level: str
    provenance: str
    data_classification: str

    @model_validator(mode="after")
    def validate_discriminated_result(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        if not self.ok and (self.data is not None or self.error is None):
            raise ValueError("failed tool results require an error and no data")
        return self


class PrincipalAttributes(ValueModel):
    organization: str | None = None
    region: str | None = None
    labels: frozenset[str] = frozenset()


class Principal(ValueModel):
    subject_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    kind: Literal["human", "service"]
    roles: tuple[str, ...]
    attributes: PrincipalAttributes = PrincipalAttributes()
    authn_method: str = Field(min_length=1)


class ResourceRef(ValueModel):
    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class AuthorizationContext(ValueModel):
    correlation_id: str
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class AuthorizationRequest(ValueModel):
    actor: Principal
    executor: Principal
    action: str = Field(min_length=1)
    resource: ResourceRef
    context: AuthorizationContext


class PolicyDecision(ValueModel):
    allow: bool
    constraints: dict[str, JsonValue] = Field(default_factory=dict)
    policy_version: str
    reason: str


class ACLMetadata(ValueModel):
    allowed_subject_ids: tuple[str, ...] = ()
    allowed_roles: tuple[str, ...] = ()
    classification: str = "internal"


class QueryFilters(ValueModel):
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class Doc(ValueModel):
    id: str
    version: str
    tenant_id: str
    content: str
    metadata: dict[str, JsonValue]
    score: float
    source_uri: str
    acl: ACLMetadata


class MemoryItem(ValueModel):
    id: str
    tenant_id: str
    subject_id: str
    content: str
    provenance: str
    confidence: float = Field(ge=0, le=1)
    sensitivity: str
    expires_at: datetime | None = None
    score: float


class GuardrailResult(ValueModel):
    passed: bool
    reason: str | None = None
    action: Literal["block", "redact", "warn"]


class NodeError(ValueModel):
    code: str
    safe_message: str


class NodeResult(ValueModel):
    status: Literal["completed", "failed", "waiting"]
    updates: dict[str, JsonValue] = Field(default_factory=dict)
    error: NodeError | None = None


class InboundRequest(ValueModel):
    headers: dict[str, str]
    raw_body: bytes = Field(max_length=1_048_576, exclude=True, repr=False)
    received_at: datetime
    request_id: str = Field(min_length=1)
    remote_addr: str | None = None

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return value


class IngressContext(ValueModel):
    executor: Principal
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class InboundMessage(ValueModel):
    source: Literal["cli", "feishu", "dingtalk"]
    source_message_id: str = Field(min_length=1)
    mapped_tenant_id: str = Field(min_length=1)
    mapped_subject_id: str = Field(min_length=1)
    sender_ref: str = Field(min_length=1)
    target_ref: str | None = None
    text: str
    received_at: datetime
    verified: Literal[True]
    dedupe_key: str = Field(min_length=1)

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return value


class SendResult(ValueModel):
    ok: bool
    message_id: str | None = None
    error: str | None = None


class ServiceHealth(ValueModel):
    ready: bool
    detail: str | None = None


class SecretValue(ValueModel):
    """Serializable secret holder whose repr and JSON value stay redacted."""

    value: SecretStr = Field(repr=False)
