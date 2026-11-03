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
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from stram_quant_mcp.audit import AuditJournal
from stram_quant_mcp.fastmcp_auth import StrictVerifier
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from stram_quant_mcp.settings import AppSettings
from tools import keycloak_negative_tokens, staging_acceptance

ROOT = Path(__file__).parents[2]
INSTALLER = ROOT / "deploy/stram_quant_siddu.sh"
COMPLETION = ROOT / "deploy/complete_siddu_staging.sh"
REALM = ROOT / "deploy/keycloak/stram-staging-realm.json"


def _strict_verifier(settings: AppSettings, private_key: rsa.RSAPrivateKey) -> StrictVerifier:
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return StrictVerifier(settings, public_key=public_pem)


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
    subprocess.run(["/usr/bin/bash", "-n", str(COMPLETION)], check=True)  # noqa: S603
    text = INSTALLER.read_text()

    assert "start-dev" not in text
    assert "STRAM_MCP_ALLOW_INSECURE_HTTP=1" not in text
    assert "M6_STAGING_AUTHORIZED" in text
    assert "FIREWALL_EGRESS_IPV4_IPV6_VERIFIED" in text
    assert "APPROVED_IMAGE_REF" in text
    assert "ROLLBACK_IMAGE_REF" in text
    assert "PublishPort=127.0.0.1:8800:8000" in text
    assert "AddHost=$MCP_ALLOWED_HOSTS:$PRIVATE_HOST_IPV4" in text
    assert "HealthCmd=/usr/bin/pg_isready" in text
    assert '--internal "$AUTH_NETWORK"' in text
    assert "Volume=$LAKE:/lake:ro,nosuid,nodev,noexec" in text
    assert "Ulimit=stack=2097152:2097152" in text
    assert "PodmanArgs=--cpus=2" in text
    assert "Environment=MALLOC_ARENA_MAX=2" in text
    assert "Environment=GLIBC_TUNABLES=glibc.malloc.arena_max=2" in text
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
        "provision-auth",
        "import-images",
        "auth-prepare",
        "install-egress",
        "auth-start",
        "auth-verify",
        "prepare-acceptance",
        "activate",
        "verify",
        "reliability-drill",
        "backup-audit",
        "verify-backup",
        "install-backup-timer",
        "rollback",
        "restore-approved",
        "enable-services",
        "discovery-routes",
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
            Route("/.well-known/oauth-authorization-server/realms/stram-staging", discovery),
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
        issuer_url=AnyHttpUrl(issuer),
        resource_url=AnyHttpUrl(resource),
        jwks_url=AnyHttpUrl(jwks_url),
        tenant_id="local-stram",
        allowed_hosts=(f"localhost:{mcp_port}",),
        allow_insecure_http=False,
        image_digest="sha256:" + "a" * 64,
    )
    served_app = create_fastmcp_application(
        settings, token_verifier=_strict_verifier(settings, private_key), audit_key=audit_key
    ).app

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
            served_app,
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

    from stram_data.data_interface import DataInterface, DerivedQuery

    interface = DataInterface(parquet_root=ROOT / "tests/fixtures/lake")
    catalog = interface.get_catalog(limit=1)
    assert catalog
    first_observation = catalog[0].first_observation
    indicators = interface.list_indicators()
    assert indicators
    indicator = indicators[0]
    subjects = interface.list_indicator_subjects(indicator.indicator_id)
    derived = interface.get_derived(
        DerivedQuery(
            indicator_id=indicator.indicator_id,
            subject=subjects[0] if subjects else None,
            start_date=indicator.first_observation,
            end_date=indicator.first_observation,
            limit=1,
        )
    )
    assert derived.rows
    event_id = str(derived.rows[0].event_id)

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
                        "arguments": {"event_id": event_id},
                    },
                    {
                        "class": "analytics",
                        "tool": "quant_get_aligned_panel",
                        "arguments": {
                            "columns": [{"name": "sample", "series_key": catalog[0].series_key}],
                            "start_date": str(first_observation),
                            "end_date": str(first_observation),
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

    from stram_data.data_interface import DataInterface, DerivedQuery

    interface = DataInterface(parquet_root=real_lake)
    catalog = interface.get_catalog(limit=5)
    assert catalog
    series_key = catalog[0].series_key
    first_observation = catalog[0].first_observation
    indicators = interface.list_indicators()
    assert indicators
    indicator = indicators[0]
    subjects = interface.list_indicator_subjects(indicator.indicator_id)
    derived = interface.get_derived(
        DerivedQuery(
            indicator_id=indicator.indicator_id,
            subject=subjects[0] if subjects else None,
            start_date=indicator.first_observation,
            end_date=indicator.first_observation,
            limit=1,
        )
    )
    assert derived.rows
    event_id = str(derived.rows[0].event_id)
    obs_date = str(first_observation)

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
            Route("/.well-known/oauth-authorization-server/realms/stram-staging", discovery),
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
        issuer_url=AnyHttpUrl(issuer),
        resource_url=AnyHttpUrl(resource),
        jwks_url=AnyHttpUrl(jwks_url),
        tenant_id="local-stram",
        allowed_hosts=(f"localhost:{mcp_port}",),
        allow_insecure_http=False,
        image_digest="sha256:" + "b" * 64,
    )
    built = create_fastmcp_application(
        settings, token_verifier=_strict_verifier(settings, private_key), audit_key=audit_key
    )

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
                            "columns": [{"name": "sample", "series_key": series_key}],
                            "start_date": obs_date,
                            "end_date": obs_date,
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


def test_keycloak_client_secret_reconciliation_is_idempotent_and_scoped(
    tmp_path: Path,
) -> None:
    import base64
    from urllib.parse import parse_qs, unquote_plus

    import httpx2

    bad_secret = tmp_path / "bad.secret"
    bad_secret.write_text("unsafe+secret/with=chars" * 2 + "\n")
    bad_secret.chmod(0o400)
    with pytest.raises(RuntimeError, match="URL-safe base64") as exc_info:
        keycloak_negative_tokens._urlsafe_secret(bad_secret)
    assert "unsafe+secret" not in str(exc_info.value)

    smoke_value = "A" * 32 + "-_" + "b" * 30
    disc_value = "C" * 32 + "_-" + "d" * 30
    smoke_file = tmp_path / "smoke.secret"
    smoke_file.write_text(smoke_value + "\n")
    smoke_file.chmod(0o400)
    disc_file = tmp_path / "disc.secret"
    disc_file.write_text(disc_value + "\n")
    disc_file.chmod(0o400)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    realm_template = json.loads(REALM.read_text())
    template_clients = {c["clientId"]: c for c in realm_template["clients"]}

    stored_clients: dict[str, dict[str, object]] = {
        "uuid-smoke": {
            **template_clients["stram-quant-smoke"],
            "id": "uuid-smoke",
            "secret": "legacy+secret/with+plus==" + "x" * 40,  # pragma: allowlist secret
        },
        "uuid-disc": {
            **template_clients["stram-quant-discovery-only"],
            "id": "uuid-disc",
            "secret": disc_value,
        },
        "uuid-other": {
            "id": "uuid-other",
            "clientId": "stram-unrelated-client",
            "secret": "untouched-secret",  # pragma: allowlist secret
            "optionalClientScopes": [],
        },
    }
    put_calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path == "/admin/realms/stram-staging/clients":
            cid = request.url.params.get("clientId")
            matches = [
                {k: v for k, v in item.items() if k != "secret"}
                for item in stored_clients.values()
                if item.get("clientId") == cid or cid == "stram-quant-smoke"
            ]
            return httpx2.Response(200, json=matches)
        if request.method == "GET" and path.endswith("/client-secret"):
            uuid = path.split("/")[-2]
            return httpx2.Response(
                200,
                json={"type": "secret", "value": stored_clients[uuid]["secret"]},
            )
        if request.method == "PUT" and path.startswith("/admin/realms/stram-staging/clients/"):
            uuid = path.split("/")[-1]
            put_calls.append(uuid)
            body = json.loads(request.content.decode())
            stored_clients[uuid]["secret"] = body["secret"]
            return httpx2.Response(204)
        if (
            request.method == "POST"
            and path == "/realms/stram-staging/protocol/openid-connect/token"
        ):
            auth_hdr = request.headers.get("Authorization", "")
            raw = base64.b64decode(auth_hdr.removeprefix("Basic ")).decode()
            cid_raw, secret_raw = raw.split(":", 1)
            cid, decoded_secret = unquote_plus(cid_raw), unquote_plus(secret_raw)
            client_obj = next(
                (item for item in stored_clients.values() if item["clientId"] == cid),
                None,
            )
            if client_obj is None or client_obj["secret"] != decoded_secret:
                return httpx2.Response(401, json={"error": "invalid_client_credentials"})
            form = parse_qs(request.content.decode())
            requested_scopes = set(form.get("scope", [""])[0].split())
            allowed_scopes = set(
                cast_list
                if isinstance(cast_list := client_obj.get("optionalClientScopes"), list)
                else []
            )
            granted = " ".join(sorted(requested_scopes & allowed_scopes))
            now = int(time.time())
            token = jwt.encode(
                {
                    "iss": "https://siddu.tailc5da4c.ts.net/realms/stram-staging",
                    "aud": "https://siddu.tailc5da4c.ts.net/mcp",
                    "sub": f"service-account-{cid}",
                    "client_id": cid,
                    "tenant_id": "local-stram",
                    "scope": granted,
                    "iat": now,
                    "exp": now + 300,
                },
                private_key,
                algorithm="RS256",
                headers={"kid": "key-1"},
            )
            return httpx2.Response(200, json={"access_token": token})
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)
    with (
        httpx2.Client(transport=transport) as admin_client,
        httpx2.Client(transport=transport) as public_client,
    ):
        keycloak_negative_tokens._reconcile_seeded_client(
            admin_client,
            public_client,
            "http://127.0.0.1:9080",
            "https://siddu.tailc5da4c.ts.net",
            "stram-staging",
            "stram-quant-smoke",
            smoke_file,
            "quant:analytics quant:discover quant:lineage quant:read",
        )
        keycloak_negative_tokens._reconcile_seeded_client(
            admin_client,
            public_client,
            "http://127.0.0.1:9080",
            "https://siddu.tailc5da4c.ts.net",
            "stram-staging",
            "stram-quant-discovery-only",
            disc_file,
            "quant:discover",
        )
        assert put_calls == ["uuid-smoke"]
        assert stored_clients["uuid-smoke"]["secret"] == smoke_value
        assert stored_clients["uuid-disc"]["secret"] == disc_value
        assert stored_clients["uuid-other"]["secret"] == "untouched-secret"  # noqa: S105  # pragma: allowlist secret

        # Second invocation is a no-op (idempotent: zero PUT calls)
        keycloak_negative_tokens._reconcile_seeded_client(
            admin_client,
            public_client,
            "http://127.0.0.1:9080",
            "https://siddu.tailc5da4c.ts.net",
            "stram-staging",
            "stram-quant-smoke",
            smoke_file,
            "quant:analytics quant:discover quant:lineage quant:read",
        )
        assert put_calls == ["uuid-smoke"]
