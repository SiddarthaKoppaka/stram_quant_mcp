"""Transport and token behavior every implementation must share."""

from __future__ import annotations

import jwt
import pytest

from tests.parity.harness import (
    ALL_SCOPES,
    ALLOWED_ORIGIN,
    HOST,
    ISSUER,
    RESOURCE,
    ParityClient,
)

METADATA_URL = f"https://{HOST}/.well-known/oauth-protected-resource/mcp"
INITIALIZE = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "parity", "version": "1"},
}
BAD_TOKEN_CASES = {
    "wrong-audience": {"overrides": {"aud": "https://other.test/mcp"}},
    "wrong-issuer": {"overrides": {"iss": "https://evil.test/realms/x"}},
    "expired": {"overrides": {"exp": 1}},
    "missing-issued-at": {"drop": ("iat",)},
    "missing-subject": {"drop": ("sub",)},
    "missing-client": {"drop": ("client_id",)},
    "wrong-tenant": {"overrides": {"tenant_id": "other-tenant"}},
    "missing-tenant": {"drop": ("tenant_id",)},
}


def test_unauthenticated_request_gets_a_challenge_naming_the_metadata(
    parity: ParityClient,
) -> None:
    reply = parity.rpc("initialize", INITIALIZE)

    assert reply.status == 401
    assert f'resource_metadata="{METADATA_URL}"' in reply.headers["www-authenticate"]


def test_resource_metadata_names_the_resource_and_the_issuer(parity: ParityClient) -> None:
    response = parity.get("/.well-known/oauth-protected-resource/mcp")
    document = response.json()

    assert response.status_code == 200
    assert document["resource"] == RESOURCE
    assert document["authorization_servers"] == [ISSUER]
    assert document["bearer_methods_supported"] == ["header"]


@pytest.mark.parametrize("case", sorted(BAD_TOKEN_CASES))
def test_a_token_with_a_bad_claim_is_rejected(parity: ParityClient, case: str) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES), **BAD_TOKEN_CASES[case])

    assert parity.rpc("initialize", INITIALIZE, token=token).status == 401


def test_a_token_signed_by_an_unknown_key_is_rejected(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES), signing_key=parity.minter.other_key)

    assert parity.rpc("initialize", INITIALIZE, token=token).status == 401


def test_a_symmetric_forgery_is_rejected(parity: ParityClient) -> None:
    forged = jwt.encode(
        {"iss": ISSUER, "aud": RESOURCE, "scope": " ".join(ALL_SCOPES), "exp": 4_102_444_800},
        "forged-secret-forged-secret-forged",
        algorithm="HS256",
    )

    assert parity.rpc("initialize", INITIALIZE, token=forged).status == 401


def test_an_unlisted_host_is_misdirected(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    reply = parity.rpc("initialize", INITIALIZE, token=token, headers={"host": "attacker.test"})

    assert reply.status == 421


def test_an_unlisted_origin_is_forbidden(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    reply = parity.rpc(
        "initialize", INITIALIZE, token=token, headers={"origin": "https://attacker.test"}
    )

    assert reply.status == 403


def test_a_listed_origin_is_accepted(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    reply = parity.rpc("initialize", INITIALIZE, token=token, headers={"origin": ALLOWED_ORIGIN})

    assert reply.status == 200


def test_replies_are_stateless_json(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    reply = parity.rpc("initialize", INITIALIZE, token=token)

    assert reply.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in reply.headers


def test_an_oversized_request_body_is_rejected(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))
    padding = b" " * (2 * 1024 * 1024)

    reply = parity.post_raw(b"{" + padding + b"}", token)

    assert reply.status == 413


def test_liveness_and_readiness_are_public(parity: ParityClient) -> None:
    assert parity.get("/health/live").status_code == 200
    assert parity.get("/health/ready").status_code == 200
