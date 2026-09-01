"""V0.3-T02 write RBAC, separation-of-duty and approval binding security tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from oria.core.approvals import (
    ApprovalResumeRequest,
    ApprovalService,
    InMemoryApprovalRepository,
)
from oria.core.types import (
    AuthorizationContext,
    AuthorizationRequest,
    Principal,
    ResourceRef,
    RetryPolicy,
    ToolPolicy,
)
from oria.permission.local import LOCAL_POLICY_VERSION, LocalPolicyEngine

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
HASH = "sha256:" + "a" * 64


def _principal(subject_id: str, *roles: str, tenant_id: str = "tenant-a") -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        kind="human",
        roles=roles,
        authn_method="trusted-test-profile",
    )


EXECUTOR = Principal(
    subject_id="worker-a",
    tenant_id="tenant-a",
    kind="service",
    roles=("runtime",),
    authn_method="trusted-test-profile",
)
INTEGRATION_EXECUTOR = Principal(
    subject_id="integration-worker-a",
    tenant_id="tenant-a",
    kind="service",
    roles=("integration_adapter",),
    authn_method="trusted-test-profile",
)
ADMIN = _principal("admin-a", "campaign_admin")
DUAL_ROLE_ADMIN = _principal("dual-role-admin", "campaign_admin", "launch_approver")
LAUNCH_APPROVER = _principal("launch-approver-a", "launch_approver")
CONSUMER_APPROVER = _principal("consumer-approver-a", "consumer_publish_approver")
READ_ONLY = _principal("reader-a", "operator")
MERCHANT = _principal("merchant-a", "merchant")


class _VersionedPolicy:
    def __init__(self, delegate: LocalPolicyEngine) -> None:
        self.delegate = delegate
        self.version = LOCAL_POLICY_VERSION

    async def authorize(self, request: AuthorizationRequest, ctx: SimpleNamespace) -> object:
        decision = await self.delegate.authorize(request, ctx)  # type: ignore[arg-type]
        return decision.model_copy(update={"policy_version": self.version})


def _context(actor: Principal, *, executor: Principal = EXECUTOR) -> SimpleNamespace:
    return SimpleNamespace(
        actor=actor,
        executor=executor,
        tenant_id=actor.tenant_id,
        correlation_id="correlation-a",
    )


def _policy(
    *actors: Principal, confirmation_assignments: dict[str, str] | None = None
) -> LocalPolicyEngine:
    return LocalPolicyEngine(
        trusted_actors=actors,
        trusted_executors=(EXECUTOR,),
        confirmation_assignments=confirmation_assignments,
    )


def _request(
    actor: Principal,
    action: str,
    *,
    tenant_id: str = "tenant-a",
) -> AuthorizationRequest:
    return AuthorizationRequest(
        actor=actor,
        executor=EXECUTOR,
        action=action,
        resource=ResourceRef(
            resource_type="campaign",
            resource_id="campaign-1",
            tenant_id=tenant_id,
        ),
        context=AuthorizationContext(correlation_id="correlation-a"),
    )


def _tool_policy(
    *, approval_action: str = "launch_approval", approval_mode: str = "required"
) -> ToolPolicy:
    return ToolPolicy(
        risk_level="high",
        side_effect=True,
        timeout_seconds=1,
        retry_policy=RetryPolicy(),
        idempotency_scope="campaign",
        required_action="campaign:launch:request",
        resource_type="campaign",
        approval_mode=approval_mode,  # type: ignore[arg-type]
        approval_action=approval_action,
    )


@pytest.mark.asyncio
async def test_write_policy_denies_by_default_wrong_role_cross_tenant_and_context_spoofing() -> (
    None
):
    policy = _policy(ADMIN, READ_ONLY)

    unknown = await policy.authorize(_request(ADMIN, "campaign:undeclared"), _context(ADMIN))
    wrong_role = await policy.authorize(
        _request(READ_ONLY, "campaign:draft:write"), _context(READ_ONLY)
    )
    cross_tenant = await policy.authorize(
        _request(ADMIN, "campaign:draft:write", tenant_id="tenant-b"), _context(ADMIN)
    )
    mismatched = await policy.authorize(
        _request(ADMIN, "campaign:draft:write"), _context(READ_ONLY)
    )

    assert all(not result.allow for result in (unknown, wrong_role, cross_tenant, mismatched))
    allowed = await policy.authorize(_request(ADMIN, "campaign:draft:write"), _context(ADMIN))
    assert allowed.allow is True
    assert allowed.policy_version == LOCAL_POLICY_VERSION


@pytest.mark.asyncio
async def test_selection_event_apply_requires_trusted_integration_executor() -> None:
    policy = LocalPolicyEngine(
        trusted_actors=(ADMIN,),
        trusted_executors=(EXECUTOR, INTEGRATION_EXECUTOR),
    )
    resource = ResourceRef(
        resource_type="campaign",
        resource_id="campaign-1",
        tenant_id="tenant-a",
    )

    denied = await policy.authorize(
        AuthorizationRequest(
            actor=ADMIN,
            executor=EXECUTOR,
            action="selection:event:apply",
            resource=resource,
            context=AuthorizationContext(correlation_id="correlation-a"),
        ),
        _context(ADMIN),  # type: ignore[arg-type]
    )
    allowed = await policy.authorize(
        AuthorizationRequest(
            actor=ADMIN,
            executor=INTEGRATION_EXECUTOR,
            action="selection:event:apply",
            resource=resource,
            context=AuthorizationContext(correlation_id="correlation-a"),
        ),
        _context(ADMIN, executor=INTEGRATION_EXECUTOR),  # type: ignore[arg-type]
    )

    assert denied.allow is False
    assert allowed.allow is True


@pytest.mark.asyncio
async def test_confirmation_write_requires_role_and_trusted_subject_assignment() -> None:
    policy = _policy(MERCHANT, confirmation_assignments={"campaign-1": MERCHANT.subject_id})

    allowed = await policy.authorize(
        _request(
            MERCHANT,
            "confirmation:merchant:decide",
        ),
        _context(MERCHANT),
    )
    unassigned = await _policy(MERCHANT).authorize(
        _request(MERCHANT, "confirmation:merchant:decide"),
        _context(MERCHANT),
    )

    assert allowed.allow is True
    assert unassigned.allow is False


@pytest.mark.asyncio
async def test_same_subject_cannot_self_approve_even_when_it_has_both_roles() -> None:
    repository = InMemoryApprovalRepository()
    service = ApprovalService(
        repository,
        _policy(DUAL_ROLE_ADMIN),
        clock=lambda: NOW,
        id_factory=lambda: "approval-self",
    )
    approval = await service.create(
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
        expires_at=NOW + timedelta(hours=1),
        ctx=_context(DUAL_ROLE_ADMIN),  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="cannot decide"):
        await service.decide(
            tenant_id="tenant-a",
            approval_id=approval.approval_id,
            decision="approve",
            reason="synthetic approval",
            ctx=_context(DUAL_ROLE_ADMIN),  # type: ignore[arg-type]
        )
    assert (await repository.get("tenant-a", approval.approval_id)).status == "pending"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_actor_without_gate_specific_approver_role_cannot_decide() -> None:
    repository = InMemoryApprovalRepository()
    service = ApprovalService(
        repository,
        _policy(ADMIN, READ_ONLY),
        clock=lambda: NOW,
        id_factory=lambda: "approval-unauthorized",
    )
    await service.create(
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
        expires_at=NOW + timedelta(hours=1),
        ctx=_context(ADMIN),  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="not authorized"):
        await service.decide(
            tenant_id="tenant-a",
            approval_id="approval-unauthorized",
            decision="approve",
            reason=None,
            ctx=_context(READ_ONLY),  # type: ignore[arg-type]
        )


async def _approved_launch(
    *,
    now: list[datetime],
) -> tuple[ApprovalService, InMemoryApprovalRepository]:
    repository = InMemoryApprovalRepository()
    policy = _policy(ADMIN, LAUNCH_APPROVER, CONSUMER_APPROVER)
    service = ApprovalService(
        repository,
        policy,
        clock=lambda: now[0],
        id_factory=lambda: "approval-launch",
    )
    approval = await service.create(
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
        expires_at=NOW + timedelta(hours=1),
        ctx=_context(ADMIN),  # type: ignore[arg-type]
    )
    await service.decide(
        tenant_id="tenant-a",
        approval_id=approval.approval_id,
        decision="approve",
        reason="synthetic approval",
        ctx=_context(LAUNCH_APPROVER),  # type: ignore[arg-type]
    )
    return service, repository


@pytest.mark.asyncio
async def test_required_side_effect_needs_approved_binding_and_reauthorizes_on_resume() -> None:
    now = [NOW]
    service, _ = await _approved_launch(now=now)
    request = ApprovalResumeRequest(
        approval_id="approval-launch",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
    )

    approved = await service.authorize_resume(
        request=request,
        tool_policy=_tool_policy(),
        ctx=_context(ADMIN),  # type: ignore[arg-type]
    )

    assert approved is not None and approved.status == "approved"
    with pytest.raises(PermissionError, match="required"):
        await service.authorize_resume(
            request=request.model_copy(update={"approval_id": None}),
            tool_policy=_tool_policy(),
            ctx=_context(ADMIN),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"canonical_args_hash": "sha256:" + "b" * 64},
        {"checkpoint_id": "checkpoint-2"},
        {"tool_name": "OtherPlan"},
        {"approval_action": "consumer_publish_approval"},
    ],
)
async def test_changed_binding_invalidates_old_approval(mutation: dict[str, object]) -> None:
    now = [NOW]
    service, repository = await _approved_launch(now=now)
    request = ApprovalResumeRequest(
        approval_id="approval-launch",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
    ).model_copy(update=mutation)

    with pytest.raises(PermissionError, match="binding"):
        await service.authorize_resume(
            request=request,
            tool_policy=_tool_policy(),
            ctx=_context(ADMIN),  # type: ignore[arg-type]
        )
    invalidated = await repository.get("tenant-a", "approval-launch")
    assert invalidated is not None and invalidated.status == "invalidated"


@pytest.mark.asyncio
async def test_expired_and_cross_tenant_approval_cannot_resume() -> None:
    now = [NOW]
    service, repository = await _approved_launch(now=now)
    now[0] = NOW + timedelta(hours=2)
    request = ApprovalResumeRequest(
        approval_id="approval-launch",
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
    )

    with pytest.raises(PermissionError, match="expired"):
        await service.authorize_resume(
            request=request,
            tool_policy=_tool_policy(),
            ctx=_context(ADMIN),  # type: ignore[arg-type]
        )
    expired = await repository.get("tenant-a", "approval-launch")
    assert expired is not None and expired.status == "expired"

    with pytest.raises(PermissionError, match="cross-tenant"):
        await service.decide(
            tenant_id="tenant-b",
            approval_id="approval-launch",
            decision="reject",
            reason="synthetic denial",
            ctx=_context(LAUNCH_APPROVER),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_policy_version_change_invalidates_approved_binding_on_resume() -> None:
    now = [NOW]
    repository = InMemoryApprovalRepository()
    policy = _VersionedPolicy(_policy(ADMIN, LAUNCH_APPROVER))
    service = ApprovalService(
        repository,
        policy,  # type: ignore[arg-type]
        clock=lambda: now[0],
        id_factory=lambda: "approval-policy-version",
    )
    await service.create(
        approval_action="launch_approval",
        tool_name="LaunchPlan",
        canonical_args_hash=HASH,
        checkpoint_id="checkpoint-1",
        expires_at=NOW + timedelta(hours=1),
        ctx=_context(ADMIN),  # type: ignore[arg-type]
    )
    await service.decide(
        tenant_id="tenant-a",
        approval_id="approval-policy-version",
        decision="approve",
        reason=None,
        ctx=_context(LAUNCH_APPROVER),  # type: ignore[arg-type]
    )
    policy.version = "local-v2"

    with pytest.raises(PermissionError, match="binding"):
        await service.authorize_resume(
            request=ApprovalResumeRequest(
                approval_id="approval-policy-version",
                approval_action="launch_approval",
                tool_name="LaunchPlan",
                canonical_args_hash=HASH,
                checkpoint_id="checkpoint-1",
            ),
            tool_policy=_tool_policy(),
            ctx=_context(ADMIN),  # type: ignore[arg-type]
        )
    invalidated = await repository.get("tenant-a", "approval-policy-version")
    assert invalidated is not None and invalidated.status == "invalidated"
    assert invalidated.decider == LAUNCH_APPROVER.subject_id
