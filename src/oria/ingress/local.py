"""Local CLI ingress normalization with fixed community identity mapping."""

from __future__ import annotations

import hashlib

from oria.core.types import InboundMessage, InboundRequest, IngressContext
from oria.permission.local import (
    LOCAL_CLI_SUBJECT_ID,
    LOCAL_TENANT_ID,
    LOCAL_USER_SUBJECT_ID,
    local_cli_executor,
)


class IngressVerificationError(PermissionError):
    """Raised when an ingress request does not come from its trusted boundary."""


class LocalCLIIngressAdapter:
    """Normalize a trusted local CLI request without accepting identity input."""

    name = "cli"

    async def verify_and_normalize(
        self, request: InboundRequest, ingress_ctx: IngressContext
    ) -> InboundMessage:
        if ingress_ctx.request_id != request.request_id:
            raise IngressVerificationError("ingress request ID mismatch")
        if ingress_ctx.executor != local_cli_executor():
            raise IngressVerificationError("untrusted CLI executor")
        try:
            text = request.raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IngressVerificationError("CLI request body must be UTF-8") from exc
        digest = hashlib.sha256(request.raw_body).hexdigest()
        return InboundMessage(
            source="cli",
            source_message_id=request.request_id,
            mapped_tenant_id=LOCAL_TENANT_ID,
            mapped_subject_id=LOCAL_USER_SUBJECT_ID,
            sender_ref=LOCAL_CLI_SUBJECT_ID,
            target_ref=None,
            text=text,
            received_at=request.received_at,
            verified=True,
            dedupe_key=f"cli:{request.request_id}:{digest}",
        )
