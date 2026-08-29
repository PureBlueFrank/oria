"""V02-POL-01 contract tests for deny-by-default document ACL decisions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oria.core.types import (
    ACLMetadata,
    AuthorizationContext,
    AuthorizationRequest,
    Principal,
    QueryFilters,
    ResourceRef,
)
from oria.permission.local import (
    LOCAL_TENANT_ID,
    LocalPolicyEngine,
    local_cli_executor,
    local_operator,
)

pytestmark = pytest.mark.contract


def _request(
    *,
    actor: Principal | None = None,
    executor: Principal | None = None,
    action: str = "document:read",
    tenant_id: str = LOCAL_TENANT_ID,
) -> AuthorizationRequest:
    values = {
        "actor": actor or local_operator(),
        "executor": executor or local_cli_executor(),
        "action": action,
        "resource": ResourceRef(
            resource_type="document",
            resource_id="document-a",
            tenant_id=tenant_id,
        ),
        "context": AuthorizationContext(correlation_id="correlation-a"),
    }
    return AuthorizationRequest.model_validate(values)


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        actor=local_operator(),
        executor=local_cli_executor(),
        correlation_id="correlation-a",
    )


@pytest.mark.asyncio
async def test_document_read_decision_generates_policy_owned_acl_filter() -> None:
    decision = await LocalPolicyEngine().authorize(_request(), _context())

    acl_filter = decision.require_acl_filter()
    assert decision.allow is True
    assert acl_filter.tenant_id == LOCAL_TENANT_ID
    assert acl_filter.allows(
        tenant_id=LOCAL_TENANT_ID,
        acl=ACLMetadata(allowed_roles=("operator",), classification="restricted"),
        classification="restricted",
    )
    assert not acl_filter.allows(
        tenant_id=LOCAL_TENANT_ID,
        acl=ACLMetadata(allowed_subject_ids=("other-subject",)),
        classification="internal",
    )
    with pytest.raises(ValueError, match="reserved"):
        acl_filter.and_query_filters(QueryFilters(attributes={"allowed_roles": ["untrusted-role"]}))


@pytest.mark.asyncio
async def test_missing_principals_context_mismatch_unknown_action_and_cross_tenant_deny() -> None:
    policy = LocalPolicyEngine()
    context = _context()
    missing_request = AuthorizationRequest.model_construct(
        actor=None,
        executor=None,
        action="document:read",
        resource=ResourceRef(
            resource_type="document",
            resource_id="document-a",
            tenant_id=LOCAL_TENANT_ID,
        ),
        context=AuthorizationContext(correlation_id="correlation-a"),
    )
    attacker = Principal(
        subject_id="synthetic-attacker",
        tenant_id=LOCAL_TENANT_ID,
        kind="human",
        roles=("operator",),
        authn_method="synthetic-test",
    )

    missing = await policy.authorize(missing_request, context)
    missing_context = await policy.authorize(
        _request(),
        SimpleNamespace(actor=None, executor=None, correlation_id="correlation-a"),
    )
    mismatch = await policy.authorize(_request(actor=attacker), context)
    unknown = await policy.authorize(_request(action="document:export"), context)
    cross_tenant = await policy.authorize(_request(tenant_id="other-tenant"), context)

    for decision in (missing, missing_context, mismatch, unknown, cross_tenant):
        assert decision.allow is False
        assert decision.constraints == {}
        assert decision.acl_filter is None
