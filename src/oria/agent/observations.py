"""Canonical, bounded model-visible tool observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import model_validator

from oria.core.context import Context
from oria.core.types import JsonValue, ToolCall, ToolError, ToolResult, ValueModel


class ObjectReference(ValueModel):
    key: str
    media_type: Literal["application/json"] = "application/json"
    sha256: str
    byte_size: int


class ToolObservation(ValueModel):
    observation_schema_version: Literal[1] = 1
    tool_schema_version: int
    ok: bool
    data: JsonValue = None
    error: ToolError | None = None
    execution_id: str
    trust_level: str
    provenance: str
    data_classification: str
    object_ref: ObjectReference | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ToolObservation:
        if self.ok and self.error is not None:
            raise ValueError("successful observations cannot contain an error")
        if not self.ok and (self.data is not None or self.error is None):
            raise ValueError("failed observations require a safe error")
        return self


@dataclass(frozen=True, slots=True)
class BuiltObservation:
    canonical_json: str
    fingerprint: str | None
    object_ref: str | None


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def failed_tool_result(*, code: str, execution_id: str) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolError(
            code=code,
            safe_message="tool execution did not complete",
            retryable=False,
        ),
        execution_id=execution_id,
        trust_level="runtime_generated",
        provenance="oria://agent/tool-execution/v1",
        data_classification="internal",
    )


def build_observation(
    call: ToolCall,
    result: ToolResult,
    *,
    tool_schema_version: int,
    max_inline_bytes: int,
    ctx: Context,
) -> BuiltObservation:
    object_ref: ObjectReference | None = None
    object_content_sha256: str | None = None
    data = result.data
    if result.ok:
        projected = canonical_json(data).encode("utf-8")
        if len(projected) > max_inline_bytes:
            digest = hashlib.sha256(projected).hexdigest()
            key = f"{ctx.tenant_id}/agent-results/{digest}.json"
            if ctx.objects is None:
                raise RuntimeError("object store is unavailable")
            stored_ref = ctx.objects.put_bytes(key, projected, ctx)
            object_ref = ObjectReference(
                key=stored_ref,
                sha256=f"sha256:{digest}",
                byte_size=len(projected),
            )
            object_content_sha256 = object_ref.sha256
            data = {
                "truncated": True,
                "preview": "validated result stored in object store",
                "byte_size": len(projected),
            }
    observation = ToolObservation(
        tool_schema_version=tool_schema_version,
        ok=result.ok,
        data=data,
        error=result.error,
        execution_id=result.execution_id,
        trust_level=result.trust_level,
        provenance=result.provenance,
        data_classification=result.data_classification,
        object_ref=object_ref,
    )
    dumped = observation.model_dump(mode="json")
    fingerprint = None
    if result.ok:
        semantic: dict[str, JsonValue] = {
            "tool_name": call.name,
            "tool_schema_version": tool_schema_version,
            "normalized_args": call.args,
            "semantic_observation": {
                "data": None if object_ref is not None else result.data,
                "trust_level": result.trust_level,
                "provenance": result.provenance,
                "data_classification": result.data_classification,
                "object_content_sha256": object_content_sha256,
            },
        }
        fingerprint = (
            "sha256:" + hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
        )
    return BuiltObservation(
        canonical_json=canonical_json(dumped),
        fingerprint=fingerprint,
        object_ref=None if object_ref is None else object_ref.key,
    )
