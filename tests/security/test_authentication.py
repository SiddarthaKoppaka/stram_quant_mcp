from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from stram_quant_mcp.auth import CallableKeyResolver, JWTTokenVerifier, JWTVerifierConfig


@pytest.fixture
def signing_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://issuer.example",
        "aud": "https://quant.example/mcp",
        "sub": "researcher-1",
        "client_id": "quant-client",
        "tenant_id": "stram",
        "scope": "quant:discover quant:read",
        "iat": now - 1,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _verifier(public_key: object) -> JWTTokenVerifier:
    return JWTTokenVerifier(
        JWTVerifierConfig(
            issuer="https://issuer.example",
            audience="https://quant.example/mcp",
            tenant_id="stram",
            leeway_seconds=0,
        ),
        CallableKeyResolver(lambda _token: public_key),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_valid_token_is_reduced_to_verified_identity(signing_keys) -> None:
    private_key, public_key = signing_keys
    compact = jwt.encode(_claims(), private_key, algorithm="RS256")

    token = await _verifier(public_key).verify_token(compact)

    assert token is not None
    assert token.token == "verified"  # noqa: S105
    assert token.subject == "researcher-1"
    assert token.client_id == "quant-client"
    assert token.scopes == ["quant:discover", "quant:read"]
    assert compact not in repr(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://attacker.example"},
        {"aud": "https://other.example/mcp"},
        {"tenant_id": "other"},
        {"exp": 1},
        {"iat": int(time.time()) + 3600},
        {"sub": ""},
        {"client_id": ""},
        {"scope": []},
    ],
)
async def test_invalid_identity_or_time_claims_fail_closed(
    signing_keys, claim_overrides: dict[str, object]
) -> None:
    private_key, public_key = signing_keys
    compact = jwt.encode(_claims(**claim_overrides), private_key, algorithm="RS256")

    assert await _verifier(public_key).verify_token(compact) is None


@pytest.mark.asyncio
async def test_unreviewed_algorithm_is_rejected() -> None:
    secret = "a-secret-long-enough-for-this-security-test"  # noqa: S105  # pragma: allowlist secret
    compact = jwt.encode(_claims(), secret, algorithm="HS256")
    verifier = _verifier(secret.encode())

    assert await verifier.verify_token(compact) is None


@pytest.mark.asyncio
async def test_forged_signature_is_rejected(signing_keys) -> None:
    _trusted_private, trusted_public = signing_keys
    attacker_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    compact = jwt.encode(_claims(), attacker_private, algorithm="RS256")

    assert await _verifier(trusted_public).verify_token(compact) is None
