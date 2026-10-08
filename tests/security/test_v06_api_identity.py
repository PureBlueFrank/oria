"""Real asymmetric signatures and adversarial HTTP identity boundaries."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oria.api.app import create_app
from oria.api.identity import (
    AuthenticationError,
    IdentityBinding,
    IdentityDirectory,
    JWTAuthenticator,
    JWTSettings,
    file_directory,
)
from oria.api.resources import authorize_resource
from oria.api.server import build_api
from oria.cli import app
from oria.core.types import Principal, ResourceRef
from oria.permission.local import LocalPolicyEngine

pytestmark = pytest.mark.security


def binding(issuer="https://identity.example", subject="user"):
    return IdentityBinding(
        issuer=issuer,
        subject=subject,
        actor=Principal(
            subject_id="actor",
            tenant_id="tenant",
            kind="human",
            roles=("operator", "launch_approver"),
            authn_method="verified-external",
        ),
        executor=Principal(
            subject_id="api",
            tenant_id="tenant",
            kind="service",
            roles=("runtime",),
            authn_method="server-config",
        ),
    )


@pytest.fixture
def identity_setup():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="key-1", alg="RS256", use="sig")
    state = {"directory": IdentityDirectory(bindings=(binding(),)), "keys": [jwk], "requests": 0}

    def serve(request):
        state["requests"] += 1
        assert str(request.url) == "https://identity.example/keys"
        return httpx.Response(200, json={"keys": state["keys"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    auth = JWTAuthenticator(
        JWTSettings(
            issuer="https://identity.example",
            audience="oria",
            jwks_url="https://identity.example/keys",
        ),
        lambda: state["directory"],
        client,
    )

    def token(**updates):
        claims = {
            "iss": "https://identity.example",
            "aud": "oria",
            "sub": "user",
            "iat": int(time.time()),
            "exp": int(time.time()) + 120,
        }
        claims.update(updates)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "key-1"})

    return auth, state, token, private


@pytest.mark.asyncio
async def test_signed_tenant_roles_are_not_authorization_facts(identity_setup):
    auth, state, token, _ = identity_setup
    actor = await auth.authenticate("Bearer " + token(tenant_id="victim", roles=["admin"]))
    assert actor == binding()
    state["directory"] = IdentityDirectory()
    with pytest.raises(AuthenticationError):
        await auth.authenticate("Bearer " + token())
    await auth.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 1},
        {"iss": "https://evil.example"},
        {"aud": "other"},
        {"sub": "unmapped"},
        {"nbf": 9999999999},
        {"iat": 9999999999},
        {"exp": None},
    ],
)
async def test_invalid_claims_rejected(identity_setup, claims):
    auth, _, token, _ = identity_setup
    with pytest.raises(AuthenticationError):
        await auth.authenticate("Bearer " + token(**claims))
    await auth.client.aclose()


@pytest.mark.asyncio
async def test_algorithm_confusion_and_missing_claims(identity_setup):
    auth, _, _, private = identity_setup
    hs = jwt.encode({"sub": "user"}, "x" * 32, algorithm="HS256", headers={"kid": "key-1"})
    missing = jwt.encode({"sub": "user"}, private, algorithm="RS256", headers={"kid": "key-1"})
    for token in (hs, missing, "malformed"):
        with pytest.raises(AuthenticationError):
            await auth.authenticate("Bearer " + token)
    await auth.client.aclose()


@pytest.mark.asyncio
async def test_jwks_rotation_and_unknown_key_throttle(identity_setup):
    auth, state, token, _ = identity_setup
    await auth.authenticate("Bearer " + token())
    second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(second.public_key()))
    raw.update(kid="key-2", alg="RS256")
    state["keys"] = [raw]
    auth._attempted_at -= 6
    claims = jwt.decode(token(), options={"verify_signature": False})
    rotated = jwt.encode(claims, second, algorithm="RS256", headers={"kid": "key-2"})
    assert await auth.authenticate("Bearer " + rotated) == binding()
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await auth.authenticate("Bearer " + token())
    assert state["requests"] == 2
    await auth.client.aclose()


def test_http_errors_do_not_echo_token_or_validation_input(identity_setup):
    auth, _, token, _ = identity_setup
    with TestClient(create_app(authenticator=auth)) as client:
        response = client.get("/v1/me", headers={"Authorization": "Bearer private-secret"})
        assert response.status_code == 401
        assert "private-secret" not in response.text
        assert set(response.json()) == {"code", "message", "request_id", "retryable", "details"}
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
        result = client.get(
            "/v1/me", headers={"Authorization": "Bearer " + token(), "X-Tenant-ID": "victim"}
        )
        assert result.json()["actor"]["tenant_id"] == "tenant"
        assert client.get("/v1/jobs/unknown").json()["code"] == "not_found"


def test_local_profile_is_explicit_and_rejects_non_loopback():
    with pytest.raises(ValueError):
        build_api(config=None, local=False, host="127.0.0.1")
    with pytest.raises(ValueError):
        build_api(config=None, local=True, host="0.0.0.0")
    application = build_api(config=None, local=True, host="127.0.0.1")
    with TestClient(application, client=("127.0.0.1", 1234)) as client:
        assert client.get("/v1/me").status_code == 200
    with TestClient(application, client=("203.0.113.1", 1234)) as client:
        assert client.get("/v1/me", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    result = CliRunner().invoke(app, ["api", "serve", "--local", "--host", "0.0.0.0"])
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_resource_policy_revocation_tenant_and_separation():
    identity = binding()
    directory = IdentityDirectory(bindings=(identity,))
    ctx = SimpleNamespace(
        actor=identity.actor,
        executor=identity.executor,
        correlation_id="request",
        policy=LocalPolicyEngine(
            trusted_actors=[identity.actor], trusted_executors=[identity.executor]
        ),
    )
    resource = ResourceRef(resource_type="approval", resource_id="approval-1", tenant_id="tenant")
    kwargs = dict(
        binding=identity,
        directory=lambda: directory,
        ctx=ctx,
        resource=resource,
        action="approval:launch:decide",
        requester="another-user",
        resource_attributes={},
    )
    await authorize_resource(**kwargs)
    with pytest.raises(PermissionError):
        await authorize_resource(**(kwargs | {"resource_attributes": {"region": "restricted"}}))
    with pytest.raises(PermissionError):
        await authorize_resource(**(kwargs | {"requester": "actor"}))
    with pytest.raises(PermissionError):
        await authorize_resource(
            **(kwargs | {"resource": resource.model_copy(update={"tenant_id": "victim"})})
        )
    with pytest.raises(PermissionError):
        await authorize_resource(**(kwargs | {"action": "unknown:action"}))
    directory = IdentityDirectory()
    with pytest.raises(AuthenticationError):
        await authorize_resource(**kwargs)


def test_directory_reload_revocation_and_conflicting_principal(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(IdentityDirectory(bindings=(binding(),)).model_dump_json())
    load = file_directory(path)
    assert load().resolve(binding().issuer, "user") == binding()
    path.write_text('{"bindings": []}')
    with pytest.raises(AuthenticationError):
        load().resolve(binding().issuer, "user")
    forged = binding().model_copy(
        update={
            "subject": "another",
            "actor": binding().actor.model_copy(update={"roles": ("admin",)}),
        }
    )
    with pytest.raises(ValueError, match="conflicting"):
        IdentityDirectory(bindings=(binding(), forged))
