"""Trusted issuance and authorized resolution of persistent approval card references."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.api.approvals import approval_revision, approval_view
from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.approvals import Approval, ApprovalConflict, ApprovalRepository, ApprovalService
from oria.core.context import Context, RuntimeServices
from oria.core.types import AuthorizationRequest, PolicyDecision
from oria.ingress.feishu import CardAction, VerifiedFeishuEvent
from oria.ingress.receipts import AcceptanceDenied, AcceptanceOutcome, IngressUnavailable
from oria.permission.local import LocalPolicyEngine
from oria.presentation.im import IMAction, IMViewModel
from oria.storage.database import set_session_tenant_context
from oria.storage.platform import PlatformRepositoryError, SQLiteApprovalRepository


def _detail_view(
    approval: Approval, event: VerifiedFeishuEvent, row: dict[str, object]
) -> IMViewModel:
    assert event.action is not None
    page = event.action.page or 1
    view = approval_view(approval)
    pages = max(1, (len(view.sections) + 4) // 5)
    if not 1 <= page <= pages:
        raise PermissionError("approval card unavailable")
    navigation = tuple(
        IMAction(label=label, value=event.action.model_copy(update={"page": target}))
        for label, target in (("上一页", page - 1), ("下一页", page + 1))
        if 1 <= target <= pages
    )
    updates: dict[str, object] = {
        "page": page,
        "navigation": navigation,
        "card_schema": row["card_schema"],
    }
    if approval.status == "pending" and approval_revision(approval) != row["approval_revision"]:
        updates["next_step"] = "审批方案已更新, 请联系发起人重新发送当前版本的审批卡片。"
    # The action family shares a random 256-bit prefix. Only rows stored by the
    # server grant an intent; deriving a sibling reference grants no new permission.
    # Reuse the original controls on detail pages instead of minting tokens on retry.
    if (
        approval.status == "pending"
        and row["card_schema"] == "2.0"
        and approval_revision(approval) == row["approval_revision"]
        and event.action.action_ref.endswith("-view")
    ):
        prefix = event.action.action_ref.removesuffix("-view")
        updates.update(
            actions=(IMAction(label="批准", value=CardAction(action_ref=f"{prefix}-approve")),),
            rejection_action=CardAction(action_ref=f"{prefix}-reject"),
        )
    return view.model_copy(update=updates)


def _validate_input(row: dict[str, object], event: VerifiedFeishuEvent) -> None:
    assert event.action is not None
    intent = row["decision_intent"]
    if intent not in {"approve", "reject", "view"}:
        raise PermissionError("approval card unavailable")
    if intent == "reject":
        if event.rejection_reason is None or not event.rejection_reason.strip():
            raise PermissionError("approval rejection requires a reason")
    elif event.rejection_reason is not None:
        raise PermissionError("unexpected approval form")
    if intent != "view" and event.action.page is not None:
        raise PermissionError("unexpected approval page")


class _TransactionalLocalPolicy:
    """Use the unchanged local Policy decision while the final approval audit is atomic."""

    def __init__(self, policy: LocalPolicyEngine) -> None:
        self._policy = policy

    async def authorize(self, request: AuthorizationRequest, ctx: Context) -> PolicyDecision:
        decision = await self._policy.evaluate(request, ctx)
        if not decision.allow:
            raise AcceptanceDenied(
                "approval card unavailable",
                lambda: self._policy.audit_decision(request, ctx, decision),
            )
        return decision


def _context(
    runtime: RuntimeServices,
    directory: Callable[[], IdentityDirectory],
    identity: IdentityBinding,
    correlation_id: str,
) -> Context:
    if directory().resolve(identity.issuer, identity.subject) != identity:
        raise AuthenticationError("identity changed")
    return runtime.new_context(
        actor=identity.actor,
        executor=identity.executor,
        session_id=correlation_id,
        thread_id=correlation_id,
        run_id=correlation_id,
        correlation_id=correlation_id,
    )


async def _binding_row(
    session: AsyncSession,
    event: VerifiedFeishuEvent,
    *,
    now: datetime,
) -> dict[str, object]:
    identity = event.binding
    if event.kind != "action" or event.action is None:
        raise PermissionError("approval card unavailable")
    if (
        event.expires_at <= now.timestamp()
        or event.message.mapped_tenant_id != identity.actor.tenant_id
        or event.message.mapped_subject_id != identity.actor.subject_id
        or event.message.sender_ref != identity.subject
    ):
        raise PermissionError("approval card unavailable")
    await set_session_tenant_context(session, identity.actor.tenant_id)
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM approval_card_bindings WHERE tenant_id = :tenant_id "
                    "AND action_hash = :action_hash"
                ),
                {
                    "tenant_id": identity.actor.tenant_id,
                    "action_hash": hashlib.sha256(event.action.action_ref.encode()).hexdigest(),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or (row["issuer"], row["subject"], row["actor_id"], row["chat_id"], row["message_id"])
        != (
            identity.issuer,
            identity.subject,
            identity.actor.subject_id,
            event.message.target_ref,
            event.message.source_message_id,
        )
        or float(row["expires_at"]) <= now.timestamp()
    ):
        raise PermissionError("approval card unavailable")
    return dict(row)


class ApprovalCardBindings:
    """Internal service only; callers supply server-confirmed chat and card message IDs.

    Resolution records authorization audits, but never decides an approval or writes
    receipts/outbox. Callers still couple business acceptance to their receipt transaction.
    Job/wait binding belongs to the future orchestration service, not this reference.
    """

    def __init__(
        self,
        runtime: RuntimeServices,
        repository: ApprovalRepository,
        directory: Callable[[], IdentityDirectory],
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._repository = repository
        self._service = ApprovalService(repository, runtime.policy)
        self._directory = directory
        self._sessions = sessions
        self._clock = clock

    def _context(self, identity: IdentityBinding) -> Context:
        return _context(self._runtime, self._directory, identity, uuid.uuid4().hex)

    async def _pending(self, approval_id: str, identity: IdentityBinding) -> Approval:
        try:
            approval = await self._service.get_for_decision(
                tenant_id=identity.actor.tenant_id,
                approval_id=approval_id,
                ctx=self._context(identity),
            )
        except PlatformRepositoryError:
            raise IngressUnavailable("approval card storage unavailable") from None
        except (LookupError, PermissionError):
            raise PermissionError("approval card unavailable") from None
        self._context(identity)
        if (
            approval.status != "pending"
            or approval.expires_at <= self._clock()
            or approval.requester == identity.actor.subject_id
        ):
            raise PermissionError("approval card unavailable")
        return approval

    async def issue(
        self,
        approval_id: str,
        identity: IdentityBinding,
        *,
        chat_id: str,
        message_id: str,
        decision_intent: Literal["approve", "reject", "view"] = "approve",
        expected_revision: str | None = None,
    ) -> CardAction:
        """Issue one server-selected intent; legacy callers still issue approval only."""

        if not identity.issuer.startswith("feishu:") or not chat_id or not message_id:
            raise ValueError("verified Feishu card destination required")
        if decision_intent not in {"approve", "reject", "view"}:
            raise ValueError("unsupported approval card decision intent")
        approval = await self._pending(approval_id, identity)
        if expected_revision is not None and approval_revision(approval) != expected_revision:
            raise ApprovalConflict("approval card revision changed")
        action = CardAction(action_ref=secrets.token_urlsafe(32))
        await self._persist(
            approval, identity, chat_id, message_id, ((decision_intent, action),), schema="1.0"
        )
        return action

    async def _persist(
        self,
        approval: Approval,
        identity: IdentityBinding,
        chat_id: str,
        message_id: str,
        actions: tuple[tuple[str, CardAction], ...],
        *,
        schema: str,
    ) -> None:
        values = {
            "tenant_id": identity.actor.tenant_id,
            "approval_id": approval.approval_id,
            "approval_revision": approval_revision(approval),
            "card_schema": schema,
            "issuer": identity.issuer,
            "subject": identity.subject,
            "actor_id": identity.actor.subject_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "expires_at": approval.expires_at.timestamp(),
        }
        try:
            async with self._sessions.begin() as session:
                await set_session_tenant_context(session, identity.actor.tenant_id)
                await session.execute(
                    text(
                        "INSERT INTO approval_card_bindings "
                        "(tenant_id, action_hash, approval_id, approval_revision, issuer, subject, "
                        "decision_intent, actor_id, chat_id, message_id, expires_at, card_schema) "
                        "VALUES "
                        "(:tenant_id, :action_hash, :approval_id, :approval_revision, "
                        ":issuer, :subject, :decision_intent, "
                        ":actor_id, :chat_id, :message_id, :expires_at, :card_schema)"
                    ),
                    [
                        {
                            **values,
                            "decision_intent": intent,
                            "action_hash": hashlib.sha256(action.action_ref.encode()).hexdigest(),
                        }
                        for intent, action in actions
                    ],
                )
                self._context(identity)
        except SQLAlchemyError:
            raise IngressUnavailable("approval card storage unavailable") from None

    async def pending_view(self, approval_id: str, identity: IdentityBinding) -> IMViewModel:
        """Authorize the recipient before disclosing any approval summary to a channel."""
        return approval_view(await self._pending(approval_id, identity)).model_copy(
            update={"card_schema": "2.0"}
        )

    async def issue_view(
        self, approval_id: str, identity: IdentityBinding, *, chat_id: str, message_id: str
    ) -> IMViewModel:
        approval = await self._pending(approval_id, identity)
        if not identity.issuer.startswith("feishu:") or not chat_id or not message_id:
            raise ValueError("verified Feishu card destination required")
        prefix = secrets.token_urlsafe(32)
        actions = tuple(
            (intent, CardAction(action_ref=f"{prefix}-{intent}"))
            for intent in ("approve", "reject", "view")
        )
        await self._persist(approval, identity, chat_id, message_id, actions, schema="2.0")
        self._context(identity)
        return approval_view(approval).model_copy(
            update={
                "card_schema": "2.0",
                "actions": (IMAction(label="批准", value=actions[0][1]),),
                "rejection_action": actions[1][1],
                "navigation": (IMAction(label="查看详情", value=actions[2][1]),),
            }
        )

    async def accepted_view(self, event: VerifiedFeishuEvent) -> IMViewModel:
        """Read a committed outcome, including duplicates, under current authorization."""
        identity = event.binding
        ctx = self._context(identity)
        if not isinstance(self._runtime.policy, LocalPolicyEngine):
            raise IngressUnavailable("approval card presentation requires local Policy")
        service = ApprovalService(self._repository, _TransactionalLocalPolicy(self._runtime.policy))
        try:
            async with self._sessions() as session:
                row = await _binding_row(session, event, now=self._clock())
            approval = await service.reauthorize_decider(
                tenant_id=identity.actor.tenant_id, approval_id=str(row["approval_id"]), ctx=ctx
            )
        except AcceptanceDenied as exc:
            await exc.audit()
            raise PermissionError("approval card unavailable") from None
        except (SQLAlchemyError, PlatformRepositoryError):
            raise IngressUnavailable("approval card storage unavailable") from None
        self._context(identity)
        if row["decision_intent"] == "view":
            return _detail_view(approval, event, row)
        if approval.status == "pending":
            raise PermissionError("approval card outcome unavailable")
        view = approval_view(approval).model_copy(update={"card_schema": row["card_schema"]})
        if row["card_schema"] == "2.0" and event.action is not None:
            prefix, _, intent = event.action.action_ref.rpartition("-")
            if intent in {"approve", "reject"}:
                view = view.model_copy(
                    update={
                        "navigation": (
                            IMAction(
                                label="查看详情", value=CardAction(action_ref=f"{prefix}-view")
                            ),
                        )
                    }
                )
        return view

    async def resolve(self, event: VerifiedFeishuEvent) -> Approval:
        identity = event.binding
        self._context(identity)
        try:
            async with self._sessions() as session:
                row = await _binding_row(session, event, now=self._clock())
        except SQLAlchemyError:
            raise IngressUnavailable("approval card storage unavailable") from None
        approval = await self._pending(str(row["approval_id"]), identity)
        if approval_revision(approval) != row["approval_revision"]:
            raise PermissionError("approval card unavailable")
        return approval


class TransactionalApprovalCardAcceptor:
    """Decide or authorize details entirely inside the receiver transaction."""

    def __init__(
        self,
        runtime: RuntimeServices,
        repository: SQLiteApprovalRepository,
        directory: Callable[[], IdentityDirectory],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(runtime.policy, LocalPolicyEngine):
            raise ValueError("transactional approval cards require the local Policy engine")
        self._runtime = runtime
        self._repository = repository
        self._directory = directory
        self._clock = clock
        self._policy = _TransactionalLocalPolicy(runtime.policy)

    async def accept(
        self,
        session: AsyncSession,
        event: VerifiedFeishuEvent,
        receipt_ref: str,
        *,
        duplicate: bool,
    ) -> AcceptanceOutcome | None:
        identity = event.binding
        ctx = _context(self._runtime, self._directory, identity, receipt_ref)
        try:
            row = await _binding_row(session, event, now=self._clock())
        except SQLAlchemyError:
            raise IngressUnavailable("approval card storage unavailable") from None
        _validate_input(row, event)
        service = ApprovalService(
            self._repository.bind(session),
            self._policy,
            clock=self._clock,
        )
        try:
            approval = await service.reauthorize_decider(
                tenant_id=identity.actor.tenant_id,
                approval_id=str(row["approval_id"]),
                ctx=ctx,
            )
            if row["decision_intent"] == "view":
                _detail_view(approval, event, row)
                return None
            # A committed duplicate revalidates current ownership/Policy/SOD, but the
            # original pending revision is no longer expected to match the terminal row.
            if duplicate:
                return None
            if approval_revision(approval) != row["approval_revision"]:
                raise ApprovalConflict("approval card revision changed")
            outcome = await service.decide_outcome(
                tenant_id=identity.actor.tenant_id,
                approval_id=approval.approval_id,
                decision="reject" if row["decision_intent"] == "reject" else "approve",
                reason=event.rejection_reason,
                ctx=ctx,
                expected=approval,
            )
        except AcceptanceDenied:
            raise
        except PlatformRepositoryError:
            raise IngressUnavailable("approval card storage unavailable") from None
        except (LookupError, PermissionError):
            raise PermissionError("approval card unavailable") from None
        except ValueError as exc:
            if isinstance(exc, ApprovalConflict):
                raise
            raise ApprovalConflict("approval card cannot be decided") from None
        if outcome.rejection is not None:
            return AcceptanceOutcome(PermissionError("approval card unavailable"))
        return None
