from __future__ import annotations

import time

import httpx2
import pytest

from stram_quant_mcp.fastmcp_auth import StrictVerifier, build_auth_provider, warm_jwks
from stram_quant_mcp.tool_registry import TOOL_SPECS
from tests.parity.harness import ALL_SCOPES, TENANT, TokenMinter, parity_settings

JWKS_URL = "https://idp.test/jwks"


def _transport(status: int, body: object) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda _request: httpx2.Response(status, json=body))


@pytest.mark.asyncio
async def test_warm_up_passes_when_the_endpoint_serves_a_key() -> None:
    await warm_jwks(JWKS_URL, transport=_transport(200, {"keys": [{"kid": "k"}]}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [(200, {"keys": []}), (200, {}), (200, ["not", "an", "object"]), (500, {"keys": [{}]})],
    ids=["no-keys", "no-field", "not-an-object", "server-error"],
)
async def test_warm_up_fails_unless_the_endpoint_serves_a_key(status: int, body: object) -> None:
    with pytest.raises(RuntimeError):
        await warm_jwks(JWKS_URL, transport=_transport(status, body))


@pytest.mark.asyncio
async def test_warm_up_fails_when_the_endpoint_is_unreachable() -> None:
    def refuse(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(RuntimeError):
        await warm_jwks(JWKS_URL, transport=httpx2.MockTransport(refuse))


@pytest.mark.asyncio
async def test_a_valid_token_yields_only_the_minimal_identity(tmp_path) -> None:
    minter = TokenMinter()
    verifier = StrictVerifier(parity_settings(tmp_path), public_key=minter.public_key_pem())
    token = minter.mint(" ".join(ALL_SCOPES))

    access = await verifier.verify_token(token)

    assert access is not None
    assert access.token != token
    assert set(access.claims) == {"iss", "tenant_id"}
    assert access.claims["tenant_id"] == TENANT
    assert set(access.scopes) == set(ALL_SCOPES)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["exp", "iat", "sub", "client_id", "tenant_id", "scope"])
async def test_a_token_missing_a_required_claim_is_rejected(tmp_path, missing: str) -> None:
    minter = TokenMinter()
    verifier = StrictVerifier(parity_settings(tmp_path), public_key=minter.public_key_pem())

    assert await verifier.verify_token(minter.mint("quant:read", drop=(missing,))) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"iat": time.time() + 600},
        {"nbf": time.time() + 600},
        {"iat": time.time() - 1, "exp": time.time() + 3_600},
    ],
    ids=["future-iat", "future-nbf", "excessive-lifetime"],
)
async def test_a_token_with_invalid_temporal_claims_is_rejected(tmp_path, overrides) -> None:
    minter = TokenMinter()
    verifier = StrictVerifier(parity_settings(tmp_path), public_key=minter.public_key_pem())

    assert await verifier.verify_token(minter.mint("quant:read", overrides=overrides)) is None


@pytest.mark.asyncio
async def test_a_client_identified_only_by_azp_is_accepted(tmp_path) -> None:
    minter = TokenMinter()
    verifier = StrictVerifier(parity_settings(tmp_path), public_key=minter.public_key_pem())
    token = minter.mint("quant:read", overrides={"azp": "keycloak-client"}, drop=("client_id",))

    access = await verifier.verify_token(token)

    assert access is not None
    assert access.client_id == "keycloak-client"


def test_more_than_one_signing_algorithm_is_refused(tmp_path) -> None:
    settings = parity_settings(tmp_path).model_copy(
        update={"allowed_algorithms": ("RS256", "ES256")}
    )

    with pytest.raises(ValueError, match="exactly one"):
        StrictVerifier(settings, public_key=b"unused")


def test_the_resource_url_must_end_in_the_mcp_path(tmp_path) -> None:
    minter = TokenMinter()
    settings = parity_settings(tmp_path).model_copy(
        update={"resource_url": "https://mcp.test/other"}
    )
    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())

    with pytest.raises(ValueError, match="/mcp"):
        build_auth_provider(settings, verifier)


def test_the_provider_advertises_every_scope_a_tool_needs(tmp_path) -> None:
    minter = TokenMinter()
    settings = parity_settings(tmp_path)
    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())

    provider = build_auth_provider(settings, verifier)

    assert provider.scopes_supported == sorted({spec.scope for spec in TOOL_SPECS})
