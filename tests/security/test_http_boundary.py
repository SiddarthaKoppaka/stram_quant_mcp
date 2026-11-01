from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from stram_quant_mcp.server import create_application
from stram_quant_mcp.settings import AppSettings


class StaticVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "valid-test-token":  # noqa: S105
            return None
        return AccessToken(
            token="verified",  # noqa: S106
            client_id="quant-client",
            scopes=["quant:discover"],
            expires_at=4_102_444_800,
            resource="http://testserver/mcp",
            subject="researcher-1",
            claims={"iss": "http://testserver/issuer", "tenant_id": "stram"},
        )


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        parquet_root=Path(__file__).parents[1] / "fixtures/lake",
        audit_db_path=tmp_path / "audit.sqlite3",
        audit_key_path=tmp_path / "audit.key",
        issuer_url="http://testserver/issuer",
        resource_url="http://testserver/mcp",
        jwks_url="http://testserver/jwks",
        tenant_id="stram",
        allowed_hosts=("testserver",),
        allowed_origins=("https://client.example",),
        allow_insecure_http=True,
        image_digest="sha256:test",
    )


def _audit_outcomes(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        records = connection.execute("SELECT record_json FROM audit_records ORDER BY sequence")
        return [json.loads(row[0])["outcome"] for row in records]


def test_transport_denials_are_fail_closed_and_audited(tmp_path) -> None:
    settings = _settings(tmp_path)
    built = create_application(settings, token_verifier=StaticVerifier(), audit_key=b"k" * 32)
    headers = {"Authorization": "Bearer valid-test-token"}

    with TestClient(built.app, base_url="http://testserver") as client:
        missing = client.post("/mcp", json={})
        bad_host = client.post("/mcp", json={}, headers={**headers, "Host": "attacker.example"})
        bad_origin = client.post(
            "/mcp", json={}, headers={**headers, "Origin": "https://attacker.example"}
        )

    assert missing.status_code == 401
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
    assert _audit_outcomes(settings.audit_db_path) == [
        "authentication_denied",
        "transport_denied",
        "transport_denied",
    ]


def test_health_is_public_but_readiness_tracks_lifespan(tmp_path) -> None:
    settings = _settings(tmp_path)
    built = create_application(settings, token_verifier=StaticVerifier(), audit_key=b"k" * 32)

    with TestClient(built.app, base_url="http://testserver") as client:
        assert client.get("/health/live").json() == {"status": "live"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

    assert built.runtime.ready is False
