"""Feishu bot transport; authorized recipients and safe views only."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from oria.core.context import Context
from oria.core.types import AuthorizationContext, AuthorizationRequest, ResourceRef, SendResult
from oria.guardrails.output import redact_output_content
from oria.presentation.im import IMViewModel, render_card


class FeishuChannel:
    """Notifier with a server-owned tenant/recipient allowlist and no automatic retry.

    A timeout is an unknown delivery outcome, never a reason to silently resend.
    Durable delivery and token provisioning belong to the deployment/T05 assembly.
    """

    name = "feishu"

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: Callable[[str], Awaitable[SecretStr]],
        recipients: Mapping[tuple[str, str], frozenset[str]],
    ) -> None:
        self._client = client
        self._token = token
        # (tenant, chat_id) -> principals explicitly allowed to publish to that chat.
        self._recipients = dict(recipients)

    async def _send(
        self,
        target: str,
        kind: str,
        content: dict[str, Any],
        ctx: Context,
        *,
        delivery_id: str | None = None,
    ) -> SendResult:
        allowed = self._recipients.get((ctx.tenant_id, target), frozenset())
        if ctx.actor.subject_id not in allowed:
            return SendResult(ok=False, error="recipient_denied")
        decision = await ctx.policy.authorize(
            AuthorizationRequest(
                actor=ctx.actor,
                executor=ctx.executor,
                action="notification:send",
                resource=ResourceRef(
                    resource_type="im_chat", resource_id=target, tenant_id=ctx.tenant_id
                ),
                context=AuthorizationContext(correlation_id=ctx.correlation_id),
            ),
            ctx,
        )
        if not decision.allow:
            return SendResult(ok=False, error="recipient_denied")
        payload = {
            "receive_id": target,
            "msg_type": kind,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if delivery_id is not None:
            if not delivery_id or len(delivery_id) > 50:
                raise ValueError("invalid delivery ID")
            payload["uuid"] = delivery_id
        try:
            credential = await self._token(ctx.tenant_id)
            if not credential.get_secret_value():
                return SendResult(ok=False, error="credentials_unavailable")
            response = await self._client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=payload,
                headers={"Authorization": "Bearer " + credential.get_secret_value()},
                timeout=10,
                follow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                return SendResult(ok=False, error="provider_rejected")
            message_id = result["data"]["message_id"]
            if not isinstance(message_id, str) or not message_id:
                raise ValueError("invalid receipt")
            return SendResult(ok=True, message_id=message_id)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return SendResult(ok=False, error="delivery_unknown")

    async def send_message(self, target: str, text: str, ctx: Context) -> SendResult:
        if not text or len(text) > 4000:
            return SendResult(ok=False, error="use_paginated_view")
        return await self._send(target, "text", {"text": redact_output_content(text)}, ctx)

    async def send_view(
        self,
        target: str,
        view: IMViewModel,
        ctx: Context,
        *,
        page: int = 1,
        delivery_id: str | None = None,
    ) -> SendResult:
        return await self._send(
            target, "interactive", render_card(view, page=page), ctx, delivery_id=delivery_id
        )

    async def send_file(self, target: str, file_path: str, ctx: Context) -> SendResult:
        # Arbitrary filesystem uploads are not part of this channel's permission contract.
        return SendResult(ok=False, error="file_upload_not_supported")
