"""Token verification, the Keycloak provider and JWKS warm-up for the FastMCP server."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx2
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.tool_registry import TOOL_SPECS

MAX_SCOPE_LENGTH = 100
MCP_PATH = "/mcp"
VERIFIED_PLACEHOLDER = "verified"
JWKS_TIMEOUT_SECONDS = 5.0


class StrictVerifier(JWTVerifier):
    """FastMCP's JWT verifier plus the checks this service requires of every token.

    The stock verifier accepts a token with no ``iat``, no subject, no client and any tenant. This
    subclass rejects those, never retains the bearer credential, and keeps only the claims policy
    needs (issuer and tenant).
    """

    def __init__(self, settings: AppSettings, *, public_key: str | bytes | None = None) -> None:
        """Create a verifier for the configured issuer, audience and algorithm.

        Args:
            settings: Service settings naming the issuer, audience, tenant and JWKS URL.
            public_key: A fixed key to trust instead of the JWKS endpoint; used only in tests.

        Raises:
            ValueError: If more than one signing algorithm is configured.
        """
        if len(settings.allowed_algorithms) != 1:
            raise ValueError("exactly one JWT signing algorithm must be configured")
        super().__init__(
            public_key=public_key,
            jwks_uri=None if public_key else str(settings.jwks_url),
            issuer=str(settings.issuer_url),
            audience=str(settings.resource_url),
            algorithm=settings.allowed_algorithms[0],
        )
        self._settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return a minimal verified identity, or None if any requirement is not met."""
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = access.claims
        tenant = claims.get(self._settings.tenant_claim)
        client_id = claims.get("client_id") or claims.get("azp")
        scopes = tuple(dict.fromkeys(access.scopes))
        if not self._has_required_identity(claims, tenant, client_id, scopes):
            return None
        return AccessToken(
            token=VERIFIED_PLACEHOLDER,
            client_id=str(client_id),
            scopes=list(scopes),
            expires_at=access.expires_at,
            resource=str(self._settings.resource_url),
            subject=str(claims["sub"]),
            claims={"iss": str(self._settings.issuer_url), self._settings.tenant_claim: tenant},
        )

    def _has_required_identity(
        self,
        claims: dict[str, object],
        tenant: object,
        client_id: object,
        scopes: tuple[str, ...],
    ) -> bool:
        if tenant != self._settings.tenant_id:
            return False
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            return False
        if not isinstance(client_id, str) or not client_id:
            return False
        if not self._has_valid_temporal_claims(claims):
            return False
        return bool(scopes) and all(len(scope) <= MAX_SCOPE_LENGTH for scope in scopes)

    def _has_valid_temporal_claims(self, claims: dict[str, object]) -> bool:
        """Require a bounded, currently usable access token, not just a valid signature."""
        issued_at = _numeric_date(claims.get("iat"))
        expires_at = _numeric_date(claims.get("exp"))
        not_before = _numeric_date(claims.get("nbf"))
        if issued_at is None or expires_at is None:
            return False
        now = time.time()
        skew = self._settings.jwt_clock_skew_seconds
        if issued_at > now + skew or expires_at <= now - skew:
            return False
        if expires_at <= issued_at or (
            expires_at - issued_at > self._settings.jwt_max_lifetime_seconds
        ):
            return False
        return not (not_before is not None and (not_before > now + skew or not_before > expires_at))


def _numeric_date(value: object) -> float | None:
    """Return a JWT NumericDate without accepting booleans or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result == result and result not in (float("inf"), float("-inf")) else None


class QuantAuthProvider(KeycloakAuthProvider):
    """The Keycloak provider, advertising this service's scopes in its resource metadata."""

    @property
    def scopes_supported(self) -> list[str]:
        """Every scope a tool requires, so clients know what they may request."""
        return sorted({spec.scope for spec in TOOL_SPECS})


def build_auth_provider(settings: AppSettings, verifier: JWTVerifier) -> QuantAuthProvider:
    """Wrap ``verifier`` in the Keycloak provider for this service.

    Raises:
        ValueError: If the resource URL does not end in ``/mcp``.
    """
    resource = urlsplit(str(settings.resource_url))
    if resource.path != MCP_PATH:
        raise ValueError("the resource URL must end in /mcp")
    return QuantAuthProvider(
        realm_url=str(settings.issuer_url),
        base_url=f"{resource.scheme}://{resource.netloc}",
        token_verifier=verifier,
    )


async def warm_jwks(
    jwks_url: str,
    *,
    timeout_seconds: float = JWKS_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    """Fail unless the JWKS endpoint answers with at least one key.

    Raises:
        RuntimeError: If the endpoint is unreachable, answers badly or has no keys.
    """
    try:
        async with httpx2.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.get(jwks_url)
        response.raise_for_status()
        keys = response.json().get("keys")
    except (httpx2.HTTPError, ValueError, AttributeError) as error:
        raise RuntimeError("the JWKS endpoint could not be read") from error
    if not keys:
        raise RuntimeError("the JWKS endpoint contains no signing keys")
