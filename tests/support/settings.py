"""Loopback-safe service settings for tests that build the service or its invocation layer."""

from __future__ import annotations

from pathlib import Path

from stram_quant_mcp.settings import AppSettings


def make_settings(tmp_path: Path, **overrides: object) -> AppSettings:
    """Return valid insecure-loopback settings whose Data Plane is the in-process fake."""

    secret = tmp_path / "data-client.secret"
    secret.write_text("test-client-secret")
    values: dict[str, object] = {
        "data_url": "http://testserver",
        "data_token_url": "http://testserver/token",
        "data_client_id": "stram-quant-mcp-test",
        "data_client_secret_path": secret,
        "audit_db_path": tmp_path / "audit.sqlite3",
        "audit_key_path": tmp_path / "audit.key",
        "issuer_url": "http://testserver/issuer",
        "resource_url": "http://testserver/mcp",
        "jwks_url": "http://testserver/jwks",
        "tenant_id": "stram",
        "allowed_hosts": ("testserver",),
        "allow_insecure_http": True,
        "image_digest": "sha256:test",
    }
    values.update(overrides)
    return AppSettings.model_validate(values)
