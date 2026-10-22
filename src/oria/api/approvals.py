"""Authorized approval presentation and decisions using the existing domain service."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field, model_validator

from oria.api.identity import AuthenticationError, IdentityBinding, IdentityDirectory
from oria.core.approvals import Approval, ApprovalConflict, ApprovalRepository, ApprovalService
from oria.core.context import Context, RuntimeServices
from oria.core.types import ValueModel
from oria.presentation.im import IMSection, IMViewModel, render_card


class ApprovalDecisionBody(ValueModel):
    decision: Literal["approve", "reject"]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> ApprovalDecisionBody:
        if self.decision == "reject" and (self.reason is None or not self.reason.strip()):
            raise ValueError("rejection requires a reason")
        return self


def approval_revision(approval: Approval) -> str:
    """Bind the complete server record without disclosing checkpoint or actor details."""
    return hashlib.sha256(approval.model_dump_json().encode()).hexdigest()


_LABELS = {
    "launch_approval": "招商发布",
    "assortment_submission_approval": "选品提交",
    "consumer_publish_approval": "消费者渠道投放",
    "merchant_notification_approval": "商家通知",
}
_STATUS = {
    "pending": "等待审批",
    "approved": "已批准",
    "rejected": "已拒绝",
    "expired": "已过期",
    "invalidated": "已失效",
}


def approval_view(approval: Approval) -> IMViewModel:
    sections = [
        IMSection(title="操作范围", text=_LABELS[approval.approval_action]),
        IMSection(title="发起时间", text=approval.created_at.isoformat()),
        IMSection(title="有效期至", text=approval.expires_at.isoformat()),
    ]
    if approval.reason:
        sections.append(IMSection(title="审批说明", text=approval.reason))
    if approval.business_binding:
        sections.append(IMSection(title="活动编号", text=approval.business_binding.campaign_id))
    sections.extend(
        (
            IMSection(
                title="批准后续", text="批准仅记录决定，后续执行仍须重新校验权限和方案版本。"
            ),
            IMSection(
                title="拒绝后续", text="本次审批终止，不执行此项操作；修改方案后须重新申请审批。"
            ),
        )
    )
    pending = approval.status == "pending"
    return IMViewModel(
        stage="waiting_approval" if pending else "result",
        title=f"{_LABELS[approval.approval_action]}审批",
        summary=_STATUS[approval.status],
        sections=tuple(sections),
        detail_ref=approval.approval_id,
        next_step=(
            "请独立审批人核对活动方案后批准或说明拒绝原因。批准仅记录决定，后续执行仍须重新校验。"
            if pending
            else "审批决定已记录；请返回活动流程查看后续进展。"
        ),
    )


class ApprovalAPI:
    def __init__(
        self,
        runtime: RuntimeServices,
        repository: ApprovalRepository,
        directory: Callable[[], IdentityDirectory],
    ) -> None:
        self._runtime = runtime
        self._directory = directory
        self._service = ApprovalService(repository, runtime.policy)

    def _context(self, identity: IdentityBinding, request_id: str) -> Context:
        if self._directory().resolve(identity.issuer, identity.subject) != identity:
            raise AuthenticationError("identity changed")
        return self._runtime.new_context(
            actor=identity.actor,
            executor=identity.executor,
            session_id=request_id,
            thread_id=request_id,
            run_id=request_id,
        )

    async def _read(self, approval_id: str, ctx: Context) -> Approval:
        try:
            return await self._service.get_for_decision(
                tenant_id=ctx.tenant_id,
                approval_id=approval_id,
                ctx=ctx,
            )
        except (LookupError, PermissionError):
            raise PermissionError("resource unavailable") from None

    async def detail(
        self,
        approval_id: str,
        identity: IdentityBinding,
        request_id: str,
        *,
        page: int = 1,
    ) -> dict[str, Any]:
        ctx = self._context(identity, request_id)
        approval = await self._read(approval_id, ctx)
        # Directory may have changed while the repository/policy was awaited.
        self._context(identity, request_id)
        view = approval_view(approval)
        pages = max(1, math.ceil(len(view.sections) / 5))
        if not 1 <= page <= pages:
            raise PermissionError("resource unavailable")
        return {
            "detail_ref": approval.approval_id,
            "status": approval.status,
            "revision": approval_revision(approval),
            "page": page,
            "pages": pages,
            "card": render_card(view, page=page),
        }

    async def decide(
        self,
        approval_id: str,
        body: ApprovalDecisionBody,
        identity: IdentityBinding,
        request_id: str,
    ) -> dict[str, str]:
        ctx = self._context(identity, request_id)
        approval = await self._read(approval_id, ctx)
        if approval_revision(approval) != body.expected_revision:
            raise ApprovalConflict("approval changed")
        ctx = self._context(identity, request_id)
        try:
            result = await self._service.decide(
                tenant_id=ctx.tenant_id,
                approval_id=approval_id,
                decision=body.decision,
                reason=body.reason,
                ctx=ctx,
                expected=approval,
            )
        except (LookupError, PermissionError):
            raise PermissionError("resource unavailable") from None
        except ValueError:
            raise ApprovalConflict("approval cannot be decided") from None
        return {"approval_id": result.approval_id, "status": result.status}
