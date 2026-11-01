from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from stram_quant_mcp.settings import AppSettings, is_loopback_url, load_secret_file


def _values(tmp_path: Path) -> dict[str, object]:
    return {
        "parquet_root": tmp_path,
        "audit_db_path": tmp_path / "audit.db",
        "audit_key_path": tmp_path / "audit.key",
        "issuer_url": "https://issuer.example",
        "resource_url": "https://quant.example/mcp",
        "jwks_url": "https://issuer.example/jwks",
        "tenant_id": "stram",
        "allowed_hosts": ("quant.example",),
    }


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"allowed_hosts": ("*",)}, "wildcard"),
        ({"allowed_algorithms": ("HS256",)}, "asymmetric"),
        ({"issuer_url": "http://issuer.example"}, "HTTPS"),
        (
            {
                "allow_insecure_http": True,
                "issuer_url": "http://issuer.example",
                "resource_url": "http://testserver/mcp",
                "jwks_url": "http://testserver/jwks",
            },
            "loopback",
        ),
        ({"otlp_endpoint": "http://collector.example"}, "OTLP"),
    ],
)
def test_security_configuration_rejects_ambiguous_values(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    values = {**_values(tmp_path), **override}
    with pytest.raises(ValidationError, match=message):
        AppSettings.model_validate(values)


def test_environment_is_parsed_once_and_missing_values_fail(monkeypatch, tmp_path) -> None:
    for key in tuple(key for key in __import__("os").environ if key.startswith("STRAM_MCP_")):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="missing"):
        AppSettings.from_env()

    environment = {
        "STRAM_MCP_PARQUET_ROOT": str(tmp_path),
        "STRAM_MCP_AUDIT_DB": str(tmp_path / "audit.db"),
        "STRAM_MCP_AUDIT_KEY_FILE": str(tmp_path / "audit.key"),
        "STRAM_MCP_ISSUER_URL": "http://testserver/issuer",
        "STRAM_MCP_RESOURCE_URL": "http://testserver/mcp",
        "STRAM_MCP_JWKS_URL": "http://testserver/jwks",
        "STRAM_MCP_TENANT_ID": "stram",
        "STRAM_MCP_ALLOWED_HOSTS": "testserver, localhost",
        "STRAM_MCP_ALLOWED_ORIGINS": "https://client.example",
        "STRAM_MCP_ALLOW_INSECURE_HTTP": "1",
        "STRAM_MCP_IMAGE_DIGEST": "sha256:test",
        "STRAM_MCP_OTLP_ENDPOINT": "http://localhost:4318",
        "STRAM_MCP_WORKER_MAX_RESTARTS": "4",
        "STRAM_MCP_WORKER_RESTART_BACKOFF_SECONDS": "0.5",
        "STRAM_MCP_WORKER_SHUTDOWN_SECONDS": "6",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    settings = AppSettings.from_env()
    assert settings.allowed_hosts == ("testserver", "localhost")
    assert settings.image_digest == "sha256:test"
    assert str(settings.otlp_endpoint) == "http://localhost:4318"
    assert settings.worker_max_restarts == 4
    assert settings.worker_restart_backoff_seconds == 0.5
    assert settings.worker_shutdown_seconds == 6


def test_secret_file_and_loopback_helpers(tmp_path) -> None:
    secret = tmp_path / "audit.key"
    secret.write_bytes(b"k" * 32 + b"\n")
    assert load_secret_file(secret) == b"k" * 32
    secret.write_text("short")
    with pytest.raises(ValueError, match="32 bytes"):
        load_secret_file(secret)
    assert is_loopback_url("http://127.0.0.1:8000")
    assert is_loopback_url("http://testserver/mcp")
    assert not is_loopback_url("https://quant.example")
