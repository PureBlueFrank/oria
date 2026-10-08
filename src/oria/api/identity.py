"""Verified external subjects mapped to current server-owned principals."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import Field, model_validator

from oria.core.types import Principal, ValueModel


class AuthenticationError(PermissionError):
    """Never contains a token, claims, or external response body."""


class IdentityBinding(ValueModel):
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    actor: Principal
    executor: Principal

    @model_validator(mode="after")
    def validate_pair(self) -> IdentityBinding:
        if self.actor.kind != "human" or self.executor.kind != "service":
            raise ValueError("human actor and service executor are required")
        if self.actor.tenant_id != self.executor.tenant_id:
            raise ValueError("actor/executor tenant mismatch")
        if self.actor.subject_id == self.executor.subject_id:
            raise ValueError("actor and executor must be distinct")
        return self


class IdentityDirectory(ValueModel):
    bindings: tuple[IdentityBinding, ...] = ()

    @model_validator(mode="after")
    def unique_bindings(self) -> IdentityDirectory:
        keys = [(item.issuer, item.subject) for item in self.bindings]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate identity binding")
        principals: dict[tuple[str, str], Principal] = {}
        for binding in self.bindings:
            for principal in (binding.actor, binding.executor):
                key = (principal.tenant_id, principal.subject_id)
                if key in principals and principals[key] != principal:
                    raise ValueError("conflicting principal attributes")
                principals[key] = principal
        return self

    def resolve(self, issuer: str, subject: str) -> IdentityBinding:
        for binding in self.bindings:
            if binding.issuer == issuer and binding.subject == subject:
                return binding
        raise AuthenticationError("identity is not mapped")


def file_directory(path: Path) -> Callable[[], IdentityDirectory]:
    """Reload on every authorization boundary so revocation needs no restart."""

    def load() -> IdentityDirectory:
        try:
            return IdentityDirectory.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            raise AuthenticationError("identity directory unavailable") from None

    return load


class JWTSettings(ValueModel):
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    jwks_url: str

    @model_validator(mode="after")
    def trusted_https_endpoint(self) -> JWTSettings:
        for value in (self.issuer, self.jwks_url):
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.fragment:
                raise ValueError("issuer and JWKS URL must be trusted HTTPS endpoints")
        return self


class JWTAuthenticator:
    """RS256 only; fixed JWKS endpoint, bounded rotation cache, no token persistence."""

    def __init__(
        self,
        settings: JWTSettings,
        directory: Callable[[], IdentityDirectory],
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.directory = directory
        self.client = client
        self._keys: dict[str, jwt.PyJWK] = {}
        self._loaded_at = float("-inf")
        self._attempted_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            if now - self._loaded_at < 60 and kid in self._keys:
                return self._keys[kid]
            if now - self._attempted_at < 5:
                raise AuthenticationError("signing key unavailable")
            self._attempted_at = now
            try:
                async with self.client.stream(
                    "GET", self.settings.jwks_url, timeout=5, follow_redirects=False
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 65_536:
                            raise ValueError("JWKS too large")
                document = json.loads(body)
                keys: dict[str, jwt.PyJWK] = {}
                for raw in document["keys"]:
                    if raw.get("kty") != "RSA" or raw.get("use", "sig") != "sig":
                        continue
                    if raw.get("alg", "RS256") != "RS256" or "d" in raw:
                        continue
                    if "key_ops" in raw and "verify" not in raw["key_ops"]:
                        continue
                    key = jwt.PyJWK(raw, algorithm="RS256")
                    if not key.key_id or key.key_id in keys or key.key.key_size < 2048:
                        raise ValueError("invalid signing key")
                    keys[key.key_id] = key
                self._keys, self._loaded_at = keys, now
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                jwt.PyJWTError,
            ):
                raise AuthenticationError("signing keys unavailable") from None
            if kid not in self._keys:
                raise AuthenticationError("unknown signing key")
            return self._keys[kid]

    async def authenticate(self, authorization: str | None) -> IdentityBinding:
        if authorization is None or not authorization.startswith("Bearer "):
            raise AuthenticationError("bearer authentication required")
        token = authorization[7:]
        if not token or len(token) > 16_384:
            raise AuthenticationError("invalid bearer token")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise AuthenticationError("invalid signing algorithm or key")
            key = await self._key(kid)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                audience=self.settings.audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            raise AuthenticationError("invalid bearer token") from None
        # Ignore all tenant/role/URL claims: only issuer + subject select an identity.
        return self.directory().resolve(self.settings.issuer, claims["sub"])
