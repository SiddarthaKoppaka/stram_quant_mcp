"""Fail-closed, immutable runtime configuration parsed once at startup."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator


class AppSettings(BaseModel):
    """Validated service configuration; secret values are file-backed, not model fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, url_preserve_empty_path=True)

    data_url: AnyHttpUrl
    data_token_url: AnyHttpUrl
    data_client_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")
    data_client_secret_path: Path
    audit_db_path: Path
    audit_key_path: Path
    issuer_url: AnyHttpUrl
    resource_url: AnyHttpUrl
    jwks_url: AnyHttpUrl
    tenant_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    tenant_claim: str = Field(default="tenant_id", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    allowed_algorithms: tuple[str, ...] = ("RS256",)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_origins: tuple[str, ...] = Field(default=(), max_length=16)
    queue_wait_seconds: float = Field(default=2.0, gt=0, le=30)
    data_max_concurrent: int = Field(default=4, ge=1, le=32)
    data_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    data_breaker_threshold: int = Field(default=5, ge=1, le=100)
    data_breaker_cooldown_seconds: float = Field(default=15.0, gt=0, le=300)
    max_request_body_bytes: int = Field(default=1024 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    backend_shutdown_seconds: float = Field(default=5.0, gt=0, le=30)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=1000)
    denial_limit: int = Field(default=5, ge=1, le=100)
    denial_window_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_denial_principals: int = Field(default=1000, ge=1, le=100_000)
    max_rate_principals: int = Field(default=1000, ge=1, le=100_000)
    allow_insecure_http: bool = False
    image_digest: str = Field(default="development", max_length=200)
    otlp_endpoint: AnyHttpUrl | None = None
    otel_export_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    otel_metric_interval_seconds: float = Field(default=30.0, ge=5, le=300)
    telemetry_shutdown_seconds: float = Field(default=5.0, gt=0, le=15)

    @model_validator(mode="after")
    def validate_security(self) -> AppSettings:
        values = (*self.allowed_hosts, *self.allowed_origins)
        if any("*" in value for value in values):
            raise ValueError("wildcard hosts and origins are prohibited")
        if any(
            algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384"}
            for algorithm in self.allowed_algorithms
        ):
            raise ValueError("only reviewed asymmetric JWT algorithms are allowed")
        urls = (
            self.issuer_url,
            self.resource_url,
            self.jwks_url,
            self.data_url,
            self.data_token_url,
        )
        if not self.allow_insecure_http and any(url.scheme != "https" for url in urls):
            raise ValueError("issuer, resource and JWKS URLs must use HTTPS")
        if self.allow_insecure_http:
            for url in urls:
                host = (url.host or "").lower()
                if url.scheme != "http" or host not in {"127.0.0.1", "localhost", "testserver"}:
                    raise ValueError("insecure HTTP is allowed only for explicit loopback tests")
        if self.otlp_endpoint is not None:
            host = (self.otlp_endpoint.host or "").lower()
            if self.otlp_endpoint.scheme != "https" and not (
                self.allow_insecure_http and host in {"127.0.0.1", "localhost", "testserver"}
            ):
                raise ValueError("OTLP endpoint must use HTTPS or explicit loopback test HTTP")
        return self

    @classmethod
    def from_env(cls) -> AppSettings:
        """Parse the complete environment once; missing values fail startup."""

        required = {
            "data_url": "STRAM_MCP_DATA_URL",
            "data_token_url": "STRAM_MCP_DATA_TOKEN_URL",
            "data_client_id": "STRAM_MCP_DATA_CLIENT_ID",
            "data_client_secret_path": "STRAM_MCP_DATA_CLIENT_SECRET_FILE",
            "audit_db_path": "STRAM_MCP_AUDIT_DB",
            "audit_key_path": "STRAM_MCP_AUDIT_KEY_FILE",
            "issuer_url": "STRAM_MCP_ISSUER_URL",
            "resource_url": "STRAM_MCP_RESOURCE_URL",
            "jwks_url": "STRAM_MCP_JWKS_URL",
            "tenant_id": "STRAM_MCP_TENANT_ID",
            "allowed_hosts": "STRAM_MCP_ALLOWED_HOSTS",
        }
        missing = [env for env in required.values() if not os.environ.get(env)]
        if missing:
            raise ValueError("required MCP configuration is missing")
        values: dict[str, object] = {field: os.environ[env] for field, env in required.items()}
        values["allowed_hosts"] = _csv(str(values["allowed_hosts"]))
        values["allowed_origins"] = _csv(os.environ.get("STRAM_MCP_ALLOWED_ORIGINS", ""))
        values["allowed_algorithms"] = _csv(os.environ.get("STRAM_MCP_ALLOWED_ALGORITHMS", "RS256"))
        values["allow_insecure_http"] = os.environ.get("STRAM_MCP_ALLOW_INSECURE_HTTP") == "1"
        values["image_digest"] = os.environ.get("STRAM_MCP_IMAGE_DIGEST", "development")
        values["otlp_endpoint"] = os.environ.get("STRAM_MCP_OTLP_ENDPOINT") or None
        optional_numbers = {
            "data_max_concurrent": "STRAM_MCP_DATA_MAX_CONCURRENT",
            "data_connect_timeout_seconds": "STRAM_MCP_DATA_CONNECT_TIMEOUT_SECONDS",
            "data_breaker_threshold": "STRAM_MCP_DATA_BREAKER_THRESHOLD",
            "data_breaker_cooldown_seconds": "STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS",
            "backend_shutdown_seconds": "STRAM_MCP_BACKEND_SHUTDOWN_SECONDS",
            "denial_limit": "STRAM_MCP_DENIAL_LIMIT",
            "denial_window_seconds": "STRAM_MCP_DENIAL_WINDOW_SECONDS",
        }
        values.update(
            {
                field: os.environ[environment]
                for field, environment in optional_numbers.items()
                if os.environ.get(environment) is not None
            }
        )
        return cls.model_validate(values)


def load_secret_file(path: Path) -> bytes:
    """Load a non-empty secret without accepting command-line or environment values."""

    secret = path.read_bytes().strip()
    if len(secret) < 32:
        raise ValueError("audit key file must contain at least 32 bytes")
    return secret


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def is_loopback_url(value: str) -> bool:
    """Used by tests and preflight without DNS resolution."""

    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "testserver"}
