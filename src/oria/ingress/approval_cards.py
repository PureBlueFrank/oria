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

from oria.api.approvals import approval_revision
from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.approvals import Approval, ApprovalConflict, ApprovalRepository, ApprovalService
from oria.core.context import Context, RuntimeServices
from oria.core.types import AuthorizationRequest, PolicyDecision
from oria.ingress.feishu import CardAction, VerifiedFeishuEvent
from oria.ingress.receipts import AcceptanceDenied, AcceptanceOutcome, IngressUnavailable
from oria.permission.local import LocalPolicyEngine
from oria.storage.database import set_session_tenant_context
from oria.storage.platform import PlatformRepositoryError, SQLiteApprovalRepository


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
        decision_intent: Literal["approve"] = "approve",
    ) -> CardAction:
        """Issue one approve-only reference; the default preserves the existing API."""

        if not identity.issuer.startswith("feishu:") or not chat_id or not message_id:
            raise ValueError("verified Feishu card destination required")
        if decision_intent != "approve":
            raise ValueError("unsupported approval card decision intent")
        approval = await self._pending(approval_id, identity)
        action = CardAction(action_ref=secrets.token_urlsafe(32))
        values = {
            "tenant_id": identity.actor.tenant_id,
            "action_hash": hashlib.sha256(action.action_ref.encode()).hexdigest(),
            "approval_id": approval.approval_id,
            "approval_revision": approval_revision(approval),
            "decision_intent": decision_intent,
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
                        "decision_intent, actor_id, chat_id, message_id, expires_at) VALUES "
                        "(:tenant_id, :action_hash, :approval_id, :approval_revision, "
                        ":issuer, :subject, :decision_intent, "
                        ":actor_id, :chat_id, :message_id, :expires_at)"
                    ),
                    values,
                )
        except SQLAlchemyError:
            raise IngressUnavailable("approval card storage unavailable") from None
        return action

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
    """Approve a server-issued card action entirely inside the receiver transaction."""

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
        if row["decision_intent"] != "approve":
            raise PermissionError("approval card unavailable")
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
            # A committed duplicate revalidates current ownership/Policy/SOD, but the
            # original pending revision is no longer expected to match the terminal row.
            if duplicate:
                return None
            if approval_revision(approval) != row["approval_revision"]:
                raise ApprovalConflict("approval card revision changed")
            outcome = await service.decide_outcome(
                tenant_id=identity.actor.tenant_id,
                approval_id=approval.approval_id,
                decision="approve",
                reason=None,
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
