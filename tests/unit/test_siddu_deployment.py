from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from stram_quant_mcp.audit import AuditJournal
from stram_quant_mcp.auth import CallableKeyResolver, JWTTokenVerifier, JWTVerifierConfig
from stram_quant_mcp.server import create_application
from stram_quant_mcp.settings import AppSettings
from tools import staging_acceptance

ROOT = Path(__file__).parents[2]
INSTALLER = ROOT / "deploy/stram_quant_siddu.sh"
REALM = ROOT / "deploy/keycloak/stram-staging-realm.json"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_self_signed_tls(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_siddu_installer_is_valid_and_keeps_activation_gates() -> None:
    # The executable and reviewed repository path are fixed test inputs.
    subprocess.run(["/usr/bin/bash", "-n", str(INSTALLER)], check=True)  # noqa: S603
    text = INSTALLER.read_text()

    assert "start-dev" not in text
    assert "STRAM_MCP_ALLOW_INSECURE_HTTP=1" not in text
    assert "M6_STAGING_AUTHORIZED" in text
    assert "FIREWALL_EGRESS_IPV4_IPV6_VERIFIED" in text
    assert "APPROVED_IMAGE_REF" in text
    assert "ROLLBACK_IMAGE_REF" in text
    assert "PublishPort=127.0.0.1:8800:8000" in text
    assert '--internal "$AUTH_NETWORK"' in text
    assert "Volume=$LAKE:/lake:ro,nosuid,nodev,noexec" in text
    assert "--entrypoint /opt/venv/bin/python" in text


def test_keycloak_realm_has_exact_claims_and_independent_scopes() -> None:
    realm = json.loads(REALM.read_text())
    assert realm["realm"] == "${STRAM_KEYCLOAK_REALM}"
    assert realm["defaultSignatureAlgorithm"] == "RS256"
    assert realm["registrationAllowed"] is False
    assert realm["bruteForceProtected"] is True

    scopes = {scope["name"]: scope for scope in realm["clientScopes"]}
    assert {"quant:discover", "quant:read", "quant:lineage", "quant:analytics"} <= scopes.keys()
    claims = scopes["stram-claims"]["protocolMappers"]
    assert {mapper["protocolMapper"] for mapper in claims} == {
        "oidc-audience-mapper",
        "oidc-hardcoded-claim-mapper",
    }
    audience = next(mapper for mapper in claims if mapper["name"] == "stram-quant-audience")
    tenant = next(mapper for mapper in claims if mapper["name"] == "stram-tenant")
    assert audience["config"]["included.custom.audience"] == "${STRAM_MCP_RESOURCE_URL}"
    assert tenant["config"]["claim.name"] == "tenant_id"
    assert tenant["config"]["claim.value"] == "${STRAM_MCP_TENANT_ID}"

    clients = {client["clientId"]: client for client in realm["clients"]}
    smoke = clients["stram-quant-smoke"]
    limited = clients["stram-quant-discovery-only"]
    assert smoke["serviceAccountsEnabled"] is True
    assert smoke["standardFlowEnabled"] is False
    assert smoke["directAccessGrantsEnabled"] is False
    assert smoke["fullScopeAllowed"] is False
    assert set(smoke["optionalClientScopes"]) == {
        "quant:discover",
        "quant:read",
        "quant:lineage",
        "quant:analytics",
    }
    assert limited["optionalClientScopes"] == ["quant:discover"]
    assert smoke["secret"] == "${STRAM_SMOKE_CLIENT_SECRET}"  # noqa: S105
    assert limited["secret"] == "${STRAM_DISCOVERY_CLIENT_SECRET}"  # noqa: S105


def test_siddu_installer_rejects_drift_and_placeholder_configs() -> None:
    text = INSTALLER.read_text()
    assert "Refusing to overwrite drifted generated file" in text
    assert "existing service.env conflicts with auth.env" in text
    assert "ROLLBACK_AUTHORIZED" in text
    assert "KCRAW_DB_PASSWORD" in text
    assert "start --optimized --import-realm" in text
    for action in (
        "doctor",
        "prepare",
        "auth-prepare",
        "auth-start",
        "auth-verify",
        "activate",
        "verify",
        "backup-audit",
        "rollback",
        "status",
    ):
        assert f"{action})" in text


def test_siddu_installer_includes_egress_seccomp_audit_backup_and_rollback_steps() -> None:
    text = INSTALLER.read_text()
    assert '("example.com", 443, "DNS")' in text
    assert '("1.1.1.1", 443, "IPv4")' in text
    assert '("2606:4700:4700::1111", 443, "IPv6")' in text
    assert "PASS: worker process has an additional seccomp filter" in text
    assert "-m stram_quant_mcp.audit backup --db /audit/audit.sqlite3" in text
    assert '"$QUANT_QUADLET.pre-rollback"' in text
    assert "as_quant systemctl --user restart stram-quant-mcp.service" in text
    assert "negative-tokens" in text
    assert "--require-complete" in text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_staging_acceptance_verifier_passes_end_to_end_over_https(tmp_path: Path) -> None:
    idp_port = _free_port()
    mcp_port = _free_port()
    issuer = f"https://localhost:{idp_port}/realms/stram-staging"
    resource = f"https://localhost:{mcp_port}/mcp"
    jwks_url = f"{issuer}/protocol/openid-connect/certs"
    token_url = f"{issuer}/protocol/openid-connect/token"

    cert_path, key_path = _write_self_signed_tls(tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "staging-key-1"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"

    async def discovery(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": issuer,
                "token_endpoint": token_url,
                "jwks_uri": jwks_url,
            }
        )

    async def jwks_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"keys": [jwk]})

    async def token_endpoint(request: Request) -> JSONResponse:
        auth_header = request.headers.get("authorization", "")
        now = int(time.time())
        scope = "quant:discover"
        client_id = "stram-quant-discovery-only"
        if "c3RyYW0tcXVhbnQtc21va2U6" in auth_header:
            scope = "quant:analytics quant:discover quant:lineage quant:read"
            client_id = "stram-quant-smoke"
        compact = jwt.encode(
            {
                "iss": issuer,
                "aud": resource,
                "sub": f"service-account-{client_id}",
                "client_id": client_id,
                "tenant_id": "local-stram",
                "scope": scope,
                "iat": now - 1,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "staging-key-1"},
        )
        return JSONResponse({"access_token": compact, "token_type": "Bearer"})

    idp_app = Starlette(
        routes=[
            Route("/realms/stram-staging/.well-known/openid-configuration", discovery),
            Route("/realms/stram-staging/protocol/openid-connect/certs", jwks_endpoint),
            Route(
                "/realms/stram-staging/protocol/openid-connect/token",
                token_endpoint,
                methods=["POST"],
            ),
        ]
    )

    audit_db = tmp_path / "audit.sqlite3"
    audit_key_file = tmp_path / "audit.key"
    audit_key = b"k" * 32
    audit_key_file.write_bytes(audit_key)
    audit_key_file.chmod(0o600)

    settings = AppSettings(
        parquet_root=ROOT / "tests/fixtures/lake",
        audit_db_path=audit_db,
        audit_key_path=audit_key_file,
        issuer_url=issuer,
        resource_url=resource,
        jwks_url=jwks_url,
        tenant_id="local-stram",
        allowed_hosts=(f"localhost:{mcp_port}",),
        allow_insecure_http=False,
        image_digest="sha256:" + "a" * 64,
    )
    verifier = JWTTokenVerifier(
        JWTVerifierConfig(
            issuer=issuer,
            audience=resource,
            tenant_id="local-stram",
            leeway_seconds=0,
        ),
        CallableKeyResolver(lambda _token: private_key.public_key()),
    )
    built = create_application(settings, token_verifier=verifier, audit_key=audit_key)

    idp_server = uvicorn.Server(
        uvicorn.Config(
            idp_app,
            host="127.0.0.1",
            port=idp_port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="error",
            access_log=False,
        )
    )
    mcp_server = uvicorn.Server(
        uvicorn.Config(
            built.app,
            host="127.0.0.1",
            port=mcp_port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="error",
            access_log=False,
        )
    )

    smoke_secret = tmp_path / "smoke.secret"
    smoke_secret.write_text("smoke-secret\n")
    smoke_secret.chmod(0o600)
    disc_secret = tmp_path / "disc.secret"
    disc_secret.write_text("disc-secret\n")
    disc_secret.chmod(0o600)

    neg_dir = tmp_path / "neg"
    neg_dir.mkdir()
    now = int(time.time())
    base_claims: dict[str, object] = {
        "iss": issuer,
        "aud": resource,
        "sub": "service-account-smoke",
        "client_id": "stram-quant-smoke",
        "tenant_id": "local-stram",
        "scope": "quant:discover quant:read",
        "iat": now - 10,
        "exp": now + 300,
    }
    overrides: dict[str, dict[str, object]] = {
        "wrong-issuer": {"iss": f"https://localhost:{idp_port}/realms/other"},
        "wrong-audience": {"aud": "https://other.invalid/mcp"},
        "wrong-tenant": {"tenant_id": "wrong-tenant"},
        "expired": {"iat": now - 600, "exp": now - 60},
    }
    for name, patch in overrides.items():
        token_claims = {**base_claims, **patch}
        token = jwt.encode(
            token_claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "staging-key-1"},
        )
        (neg_dir / f"{name}.jwt").write_text(token)

    calls_file = tmp_path / "calls.json"
    calls_file.write_text(
        json.dumps(
            {
                "calls": [
                    {"class": "discovery", "tool": "quant_list_series", "arguments": {"limit": 1}},
                    {"class": "read", "tool": "quant_get_freshness", "arguments": {}},
                    {
                        "class": "lineage",
                        "tool": "quant_get_lineage",
                        "arguments": {"event_id": "00000000-0000-0000-0000-000000000001"},
                    },
                    {
                        "class": "analytics",
                        "tool": "quant_get_aligned_panel",
                        "arguments": {
                            "series_keys": ["SPY"],
                            "start": "2026-10-01",
                            "end": "2026-10-02",
                            "availability": "strict",
                            "limit": 5,
                        },
                    },
                ]
            }
        )
    )

    idp_task = asyncio.create_task(idp_server.serve())
    mcp_task = asyncio.create_task(mcp_server.serve())
    try:
        for _ in range(100):
            if idp_server.started and mcp_server.started:
                break
            await asyncio.sleep(0.01)
        assert idp_server.started and mcp_server.started

        config = staging_acceptance.Configuration(
            endpoint=resource,
            issuer=issuer,
            resource=resource,
            tenant="local-stram",
            client_id="stram-quant-smoke",
            client_secret_file=smoke_secret,
            discovery_client_id="stram-quant-discovery-only",
            discovery_client_secret_file=disc_secret,
            calls_file=calls_file,
            negative_token_dir=neg_dir,
            ca_file=cert_path,
            require_complete=True,
        )
        rc = await staging_acceptance._run(config)
        assert rc == 0

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup_path = backup_dir / "audit-backup.sqlite3"
        journal = AuditJournal(audit_db, audit_key)
        try:
            verification = journal.backup(backup_path)
        finally:
            journal.close()
        assert verification.records >= 5
        assert verification.last_sequence == verification.records
    finally:
        mcp_server.should_exit = True
        idp_server.should_exit = True
        await asyncio.wait_for(asyncio.gather(mcp_task, idp_task), timeout=10)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_staging_acceptance_against_real_stock_lake_readonly(tmp_path: Path) -> None:
    real_lake = Path("/home/siddu/Apps/stram/data-lake/parquet")
    if not (real_lake / "_manifests/economic_observation/_active.json").is_file():
        pytest.skip("canonical real Stock lake is not mounted in this environment")

    from stram_data.data_interface import DataInterface, SeriesQuery

    interface = DataInterface(parquet_root=real_lake)
    catalog = interface.get_catalog(limit=5)
    assert catalog.entries
    series_key = catalog.entries[0].series_key
    sample = interface.get_series(
        SeriesQuery(
            series_keys=[series_key],
            start_date="2020-01-01",
            end_date="2026-11-01",
            limit=1,
        )
    )
    assert sample.observations
    event_id = str(sample.observations[0].event_id)
    obs_date = str(sample.observations[0].observation_date)

    idp_port = _free_port()
    mcp_port = _free_port()
    issuer = f"https://localhost:{idp_port}/realms/stram-staging"
    resource = f"https://localhost:{mcp_port}/mcp"
    jwks_url = f"{issuer}/protocol/openid-connect/certs"
    token_url = f"{issuer}/protocol/openid-connect/token"

    cert_path, key_path = _write_self_signed_tls(tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "staging-real-key-1"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"

    async def discovery(_request: Request) -> JSONResponse:
        return JSONResponse({"issuer": issuer, "token_endpoint": token_url, "jwks_uri": jwks_url})

    async def jwks_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"keys": [jwk]})

    async def token_endpoint(request: Request) -> JSONResponse:
        auth_header = request.headers.get("authorization", "")
        now = int(time.time())
        scope = "quant:discover"
        client_id = "stram-quant-discovery-only"
        if "c3RyYW0tcXVhbnQtc21va2U6" in auth_header:
            scope = "quant:analytics quant:discover quant:lineage quant:read"
            client_id = "stram-quant-smoke"
        compact = jwt.encode(
            {
                "iss": issuer,
                "aud": resource,
                "sub": f"service-account-{client_id}",
                "client_id": client_id,
                "tenant_id": "local-stram",
                "scope": scope,
                "iat": now - 1,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "staging-real-key-1"},
        )
        return JSONResponse({"access_token": compact, "token_type": "Bearer"})

    idp_app = Starlette(
        routes=[
            Route("/realms/stram-staging/.well-known/openid-configuration", discovery),
            Route("/realms/stram-staging/protocol/openid-connect/certs", jwks_endpoint),
            Route(
                "/realms/stram-staging/protocol/openid-connect/token",
                token_endpoint,
                methods=["POST"],
            ),
        ]
    )

    audit_db = tmp_path / "real_audit.sqlite3"
    audit_key_file = tmp_path / "real_audit.key"
    audit_key = b"r" * 32
    audit_key_file.write_bytes(audit_key)
    audit_key_file.chmod(0o600)

    settings = AppSettings(
        parquet_root=real_lake,
        audit_db_path=audit_db,
        audit_key_path=audit_key_file,
        issuer_url=issuer,
        resource_url=resource,
        jwks_url=jwks_url,
        tenant_id="local-stram",
        allowed_hosts=(f"localhost:{mcp_port}",),
        allow_insecure_http=False,
        image_digest="sha256:" + "b" * 64,
    )
    verifier = JWTTokenVerifier(
        JWTVerifierConfig(
            issuer=issuer,
            audience=resource,
            tenant_id="local-stram",
            leeway_seconds=0,
        ),
        CallableKeyResolver(lambda _token: private_key.public_key()),
    )
    built = create_application(settings, token_verifier=verifier, audit_key=audit_key)

    idp_server = uvicorn.Server(
        uvicorn.Config(
            idp_app,
            host="127.0.0.1",
            port=idp_port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="error",
            access_log=False,
        )
    )
    mcp_server = uvicorn.Server(
        uvicorn.Config(
            built.app,
            host="127.0.0.1",
            port=mcp_port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="error",
            access_log=False,
        )
    )

    smoke_secret = tmp_path / "smoke.secret"
    smoke_secret.write_text("smoke-secret\n")
    smoke_secret.chmod(0o600)
    disc_secret = tmp_path / "disc.secret"
    disc_secret.write_text("disc-secret\n")
    disc_secret.chmod(0o600)

    neg_dir = tmp_path / "neg"
    neg_dir.mkdir()
    now = int(time.time())
    base_claims: dict[str, object] = {
        "iss": issuer,
        "aud": resource,
        "sub": "service-account-smoke",
        "client_id": "stram-quant-smoke",
        "tenant_id": "local-stram",
        "scope": "quant:discover quant:read",
        "iat": now - 10,
        "exp": now + 300,
    }
    overrides: dict[str, dict[str, object]] = {
        "wrong-issuer": {"iss": f"https://localhost:{idp_port}/realms/other"},
        "wrong-audience": {"aud": "https://other.invalid/mcp"},
        "wrong-tenant": {"tenant_id": "wrong-tenant"},
        "expired": {"iat": now - 600, "exp": now - 60},
    }
    for name, patch in overrides.items():
        token = jwt.encode(
            {**base_claims, **patch},
            private_key,
            algorithm="RS256",
            headers={"kid": "staging-real-key-1"},
        )
        (neg_dir / f"{name}.jwt").write_text(token)

    calls_file = tmp_path / "real_calls.json"
    calls_file.write_text(
        json.dumps(
            {
                "calls": [
                    {"class": "discovery", "tool": "quant_list_series", "arguments": {"limit": 1}},
                    {"class": "read", "tool": "quant_get_freshness", "arguments": {}},
                    {
                        "class": "lineage",
                        "tool": "quant_get_lineage",
                        "arguments": {"event_id": event_id},
                    },
                    {
                        "class": "analytics",
                        "tool": "quant_get_aligned_panel",
                        "arguments": {
                            "series_keys": [series_key],
                            "start": obs_date,
                            "end": obs_date,
                            "availability": "strict",
                            "limit": 5,
                        },
                    },
                ]
            }
        )
    )

    idp_task = asyncio.create_task(idp_server.serve())
    mcp_task = asyncio.create_task(mcp_server.serve())
    try:
        for _ in range(100):
            if idp_server.started and mcp_server.started:
                break
            await asyncio.sleep(0.01)
        assert idp_server.started and mcp_server.started

        config = staging_acceptance.Configuration(
            endpoint=resource,
            issuer=issuer,
            resource=resource,
            tenant="local-stram",
            client_id="stram-quant-smoke",
            client_secret_file=smoke_secret,
            discovery_client_id="stram-quant-discovery-only",
            discovery_client_secret_file=disc_secret,
            calls_file=calls_file,
            negative_token_dir=neg_dir,
            ca_file=cert_path,
            require_complete=True,
        )
        rc = await staging_acceptance._run(config)
        assert rc == 0
    finally:
        mcp_server.should_exit = True
        idp_server.should_exit = True
        await asyncio.wait_for(asyncio.gather(mcp_task, idp_task), timeout=10)
