"""Feishu bot transport; authorized recipients and safe views only."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

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
        # Only confirmed shared cards sent by this instance may be patched. This is
        # a destination guard, not a delivery ledger or a reason to retry a send.
        self._shared_cards: dict[tuple[str, str, str, str], None] = {}

    async def _authorized(self, target: str, ctx: Context) -> bool:
        if ctx.actor.subject_id not in self._recipients.get((ctx.tenant_id, target), frozenset()):
            return False
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
        return decision.allow

    async def _send(
        self,
        target: str,
        kind: str,
        content: dict[str, Any],
        ctx: Context,
        *,
        delivery_id: str | None = None,
        check_identity: Callable[[], None] | None = None,
    ) -> SendResult:
        if not await self._authorized(target, ctx):
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
            if check_identity is not None:
                check_identity()
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
        shared_update: bool = False,
        check_identity: Callable[[], None] | None = None,
    ) -> SendResult:
        result = await self._send(
            target,
            "interactive",
            render_card(view, page=page, shared_update=shared_update),
            ctx,
            delivery_id=delivery_id,
            check_identity=check_identity,
        )
        if shared_update and result.ok and result.message_id:
            key = (ctx.tenant_id, target, result.message_id, ctx.actor.subject_id)
            if len(self._shared_cards) >= 1024:
                self._shared_cards.pop(next(iter(self._shared_cards)))
            self._shared_cards[key] = None
        return result

    async def update_view(
        self,
        target: str,
        message_id: str,
        view: IMViewModel,
        ctx: Context,
        *,
        page: int = 1,
        check_identity: Callable[[], None] | None = None,
    ) -> SendResult:
        """Patch a confirmed shared card; unknown outcomes never trigger another send."""
        key = (ctx.tenant_id, target, message_id, ctx.actor.subject_id)
        if key not in self._shared_cards or not await self._authorized(target, ctx):
            return SendResult(ok=False, error="recipient_denied")
        content = json.dumps(render_card(view, page=page, shared_update=True), ensure_ascii=False)
        try:
            credential = await self._token(ctx.tenant_id)
            if not credential.get_secret_value():
                return SendResult(ok=False, error="credentials_unavailable")
            if check_identity is not None:
                check_identity()
            response = await self._client.patch(
                "https://open.feishu.cn/open-apis/im/v1/messages/" + quote(message_id, safe=""),
                json={"content": content},
                headers={"Authorization": "Bearer " + credential.get_secret_value()},
                timeout=10,
                follow_redirects=False,
            )
            response.raise_for_status()
            if response.json().get("code") != 0:
                return SendResult(ok=False, message_id=message_id, error="provider_rejected")
            return SendResult(ok=True, message_id=message_id)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return SendResult(ok=False, message_id=message_id, error="delivery_unknown")

    async def send_file(self, target: str, file_path: str, ctx: Context) -> SendResult:
        # Arbitrary filesystem uploads are not part of this channel's permission contract.
        return SendResult(ok=False, error="file_upload_not_supported")
