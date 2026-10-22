"""Trusted approval-card delivery: inactive card, durable binding, then activation."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.context import Context, RuntimeServices
from oria.core.types import SendResult
from oria.ingress.approval_cards import ApprovalCardBindings
from oria.ingress.feishu_channel import FeishuChannel
from oria.ingress.receipts import IngressUnavailable


class ApprovalCardDelivery:
    """Internal service; destinations and both identities come from trusted configuration.

    No resend or background retry is performed. A partially delivered card requires
    reconciliation; durable outbound scheduling/ledger remains a T05 responsibility.
    """

    def __init__(
        self,
        runtime: RuntimeServices,
        bindings: ApprovalCardBindings,
        directory: Callable[[], IdentityDirectory],
        channel: FeishuChannel,
        recipients: Mapping[tuple[str, str], frozenset[str]],
    ) -> None:
        self._runtime = runtime
        self._bindings = bindings
        self._directory = directory
        self._channel = channel
        self._recipients = dict(recipients)

    async def deliver(
        self,
        approval_id: str,
        *,
        sender: IdentityBinding,
        approver: IdentityBinding,
        chat_id: str,
        delivery_id: str,
    ) -> SendResult:
        """Deliver one approval card; the initiator is never implicitly its approver."""
        if not delivery_id or len(delivery_id) > 50:
            raise ValueError("invalid delivery ID")

        def context() -> Context:
            directory = self._directory()
            if (
                directory.resolve(sender.issuer, sender.subject) != sender
                or directory.resolve(approver.issuer, approver.subject) != approver
            ):
                raise AuthenticationError("identity changed")
            if (
                sender.actor.tenant_id != approver.actor.tenant_id
                or not approver.issuer.startswith("feishu:")
                or approver.actor.subject_id
                not in self._recipients.get((approver.actor.tenant_id, chat_id), frozenset())
            ):
                raise PermissionError("approval card destination unavailable")
            return self._runtime.new_context(
                actor=sender.actor,
                executor=sender.executor,
                session_id=delivery_id,
                thread_id=delivery_id,
                run_id=delivery_id,
                correlation_id=delivery_id,
            )

        def check_identity() -> None:
            context()

        check_identity()
        preview = await self._bindings.pending_view(approval_id, approver)
        inactive = preview.model_copy(
            update={"next_step": "审批按钮正在准备。请等待卡片更新。", "actions": ()}
        )
        sent = await self._channel.send_view(
            chat_id,
            inactive,
            context(),
            delivery_id=delivery_id,
            shared_update=True,
            check_identity=check_identity,
        )
        if not sent.ok or not sent.message_id:
            return sent
        try:
            active = await self._bindings.issue_view(
                approval_id, approver, chat_id=chat_id, message_id=sent.message_id
            )
            ctx = context()
        except (PermissionError, ValueError, IngressUnavailable):
            # The already sent placeholder has no actions. Never silently send again.
            return SendResult(
                ok=False, message_id=sent.message_id, error="card_binding_unavailable"
            )
        try:
            return await self._channel.update_view(
                chat_id, sent.message_id, active, ctx, check_identity=check_identity
            )
        except PermissionError:
            return SendResult(
                ok=False, message_id=sent.message_id, error="card_binding_unavailable"
            )
