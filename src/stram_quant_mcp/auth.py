"""JWT verification for the MCP OAuth resource server."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol, cast

import jwt
from jwt import PyJWK, PyJWKClient
from jwt.algorithms import AllowedPublicKeys
from jwt.exceptions import PyJWKClientError, PyJWTError
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict

from stram_quant_mcp.settings import AppSettings

JWTVerificationKey = AllowedPublicKeys | PyJWK | str | bytes


class SigningKeyResolver(Protocol):
    async def resolve(self, token: str) -> JWTVerificationKey:
        """Return the reviewed public key for a compact JWT."""

    async def warmup(self) -> None:
        """Confirm initial verification key material is reachable."""


class JWKSResolver:
    """Bounded, cached JWKS key lookup using PyJWT's reviewed implementation."""

    def __init__(self, jwks_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._client = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            cache_keys=True,
            lifespan=300,
            timeout=timeout_seconds,
        )
        self._timeout_seconds = timeout_seconds

    async def resolve(self, token: str) -> JWTVerificationKey:
        lookup = asyncio.to_thread(self._client.get_signing_key_from_jwt, token)
        signing_key = await asyncio.wait_for(lookup, timeout=self._timeout_seconds)
        return cast(JWTVerificationKey, signing_key.key)

    async def warmup(self) -> None:
        lookup = asyncio.to_thread(self._client.get_jwk_set, refresh=True)
        key_set = await asyncio.wait_for(lookup, timeout=self._timeout_seconds)
        if not key_set.keys:
            raise RuntimeError("JWKS contains no signing keys")


class CallableKeyResolver:
    """Deterministic resolver for tests without network or credentials."""

    def __init__(self, resolver: Callable[[str], JWTVerificationKey]) -> None:
        self._resolver = resolver

    async def resolve(self, token: str) -> JWTVerificationKey:
        return self._resolver(token)

    async def warmup(self) -> None:
        return None


class JWTVerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    audience: str
    tenant_id: str
    tenant_claim: str = "tenant_id"
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30


class JWTTokenVerifier:
    """Validate issuer, audience, algorithm, time, identity, tenant, and scopes."""

    def __init__(self, config: JWTVerifierConfig, resolver: SigningKeyResolver) -> None:
        self._config = config
        self._resolver = resolver

    @classmethod
    def from_settings(cls, settings: AppSettings) -> JWTTokenVerifier:
        config = JWTVerifierConfig(
            issuer=str(settings.issuer_url),
            audience=str(settings.resource_url),
            tenant_id=settings.tenant_id,
            tenant_claim=settings.tenant_claim,
            algorithms=settings.allowed_algorithms,
        )
        return cls(config, JWKSResolver(str(settings.jwks_url)))

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await self._resolver.resolve(token)
            decoded = jwt.decode(
                token,
                key=key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=self._config.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            claims = cast(dict[str, object], decoded)
            tenant = claims.get(self._config.tenant_claim)
            subject = claims.get("sub")
            client_id = claims.get("client_id") or claims.get("azp")
            expires_at = claims.get("exp")
            if tenant != self._config.tenant_id:
                return None
            if not isinstance(subject, str) or not subject:
                return None
            if not isinstance(client_id, str) or not client_id:
                return None
            if not isinstance(expires_at, int):
                return None
            scopes = _parse_scopes(claims.get("scope", claims.get("scp")))
            if scopes is None:
                return None
            return AccessToken(
                # Never retain the bearer credential after verification.
                token="verified",  # noqa: S106
                client_id=client_id,
                scopes=list(scopes),
                expires_at=expires_at,
                resource=self._config.audience,
                subject=subject,
                claims={"iss": self._config.issuer, self._config.tenant_claim: tenant},
            )
        except (PyJWTError, PyJWKClientError, TimeoutError, OSError, ValueError, TypeError):
            return None

    async def warmup(self) -> None:
        await self._resolver.warmup()


def _parse_scopes(raw: object) -> tuple[str, ...] | None:
    if isinstance(raw, str):
        scopes = tuple(item for item in raw.split() if item)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        scopes = tuple(cast(list[str], raw))
    else:
        return None
    if not scopes or any(len(item) > 100 for item in scopes):
        return None
    return tuple(dict.fromkeys(scopes))
