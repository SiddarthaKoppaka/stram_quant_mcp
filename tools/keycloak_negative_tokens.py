"""Create genuine, temporary Keycloak-signed negative JWT acceptance evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import jwt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-loopback", default="http://127.0.0.1:9080")
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--bootstrap-password-file", type=Path, required=True)
    parser.add_argument("--smoke-secret-file", type=Path)
    parser.add_argument("--discovery-secret-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    return parser


def _secret(path: Path) -> str:
    value = path.read_text().strip()
    if not value or path.stat().st_mode & 0o077:
        raise RuntimeError("secret file is empty or accessible beyond its owner")
    return value


def _urlsafe_secret(path: Path) -> str:
    value = _secret(path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise RuntimeError("client secret file must contain 32-128 URL-safe base64 characters")
    return value


def _request(client: httpx2.Client, method: str, url: str, **kwargs: Any) -> httpx2.Response:
    response = client.request(method, url, **kwargs)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Keycloak operation returned HTTP {response.status_code}")
    return response


def _client_payload(
    client_id: str,
    client_secret: str,
    resource: str,
    tenant: str,
    *,
    lifespan: int | None = None,
) -> dict[str, object]:
    attributes = {
        "oauth2.device.authorization.grant.enabled": "false",
        "oidc.ciba.grant.enabled": "false",
        "use.refresh.tokens": "false",
    }
    if lifespan is not None:
        attributes["access.token.lifespan"] = str(lifespan)
    return {
        "clientId": client_id,
        "enabled": True,
        "clientAuthenticatorType": "client-secret",
        "secret": client_secret,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "publicClient": False,
        "fullScopeAllowed": False,
        "attributes": attributes,
        "protocolMappers": [
            {
                "name": "acceptance-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": resource,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
            {
                "name": "acceptance-tenant",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "config": {
                    "claim.name": "tenant_id",
                    "claim.value": tenant,
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
        ],
    }


def _token(
    client: httpx2.Client,
    public_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str | None = None,
) -> str:
    data: dict[str, str] = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    response = _request(
        client,
        "POST",
        f"{public_url}/realms/{realm}/protocol/openid-connect/token",
        data=data,
        auth=(client_id, client_secret),
    )
    value = response.json()
    token = value.get("access_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Keycloak token response omitted access_token")
    if jwt.get_unverified_header(token).get("alg") != "RS256":
        raise RuntimeError("Keycloak token is not RS256")
    return token


def _reconcile_seeded_client(
    admin_client: httpx2.Client,
    public_client: httpx2.Client,
    admin_loopback: str,
    public_url: str,
    realm: str,
    client_id: str,
    secret_path: Path,
    required_scope: str,
) -> None:
    expected_secret = _urlsafe_secret(secret_path)
    found_clients = _request(
        admin_client,
        "GET",
        f"{admin_loopback}/admin/realms/{realm}/clients",
        params={"clientId": client_id},
    ).json()
    if not isinstance(found_clients, list):
        raise RuntimeError(f"Keycloak client lookup failed for {client_id}")
    exact_matches = [
        item
        for item in found_clients
        if isinstance(item, dict)
        and item.get("clientId") == client_id
        and isinstance(item.get("id"), str)
    ]
    if len(exact_matches) != 1:
        raise RuntimeError(f"Keycloak realm must contain exactly one {client_id} client")
    client_rep = dict(exact_matches[0])
    client_uuid = str(client_rep["id"])
    secret_response = admin_client.get(
        f"{admin_loopback}/admin/realms/{realm}/clients/{client_uuid}/client-secret"
    )
    current_secret = (
        secret_response.json().get("value")
        if secret_response.status_code == 200 and isinstance(secret_response.json(), dict)
        else None
    )
    if not isinstance(current_secret, str) or not secrets.compare_digest(
        current_secret, expected_secret
    ):
        client_rep["secret"] = expected_secret
        _request(
            admin_client,
            "PUT",
            f"{admin_loopback}/admin/realms/{realm}/clients/{client_uuid}",
            json=client_rep,
        )
    token = _token(
        public_client,
        public_url,
        realm,
        client_id,
        expected_secret,
        scope=required_scope,
    )
    claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    granted = set(str(claims.get("scope", "")).split())
    if not set(required_scope.split()).issubset(granted):
        raise RuntimeError(f"Keycloak token for {client_id} omitted required scopes")


def _write(path: Path, value: str) -> None:
    path.write_text(value)
    path.chmod(0o400)


def main() -> None:
    args = _parser().parse_args()
    os.umask(0o077)
    public_url = str(args.public_url).rstrip("/")
    public_host = urlsplit(public_url).netloc
    if not public_host or urlsplit(public_url).scheme != "https":
        raise SystemExit("BLOCKED: --public-url must be HTTPS")
    output = Path(args.output_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    verify: bool | str = str(args.ca_file) if args.ca_file else True
    forwarding = {
        "Host": public_host,
        "X-Forwarded-Host": public_host,
        "X-Forwarded-Proto": "https",
    }
    temporary_realm = f"{args.realm}-negative-{secrets.token_hex(4)}"
    created_clients: list[str] = []
    realm_created = False
    with (
        httpx2.Client(timeout=15, verify=verify) as public_client,
        httpx2.Client(timeout=15, headers=forwarding) as admin_client,
    ):
        admin_token_response = _request(
            admin_client,
            "POST",
            f"{args.admin_loopback}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "stram-bootstrap-admin",
                "password": _secret(Path(args.bootstrap_password_file)),
            },
        )
        admin_token = admin_token_response.json().get("access_token")
        if not isinstance(admin_token, str) or not admin_token:
            raise SystemExit("BLOCKED: Keycloak admin token response was invalid")
        admin_client.headers["Authorization"] = f"Bearer {admin_token}"
        for seeded_client_id, secret_path, required_scope in (
            (
                "stram-quant-smoke",
                args.smoke_secret_file,
                "quant:analytics quant:discover quant:lineage quant:read",
            ),
            ("stram-quant-discovery-only", args.discovery_secret_file, "quant:discover"),
        ):
            if secret_path is None:
                continue
            _reconcile_seeded_client(
                admin_client,
                public_client,
                str(args.admin_loopback),
                public_url,
                str(args.realm),
                seeded_client_id,
                Path(secret_path),
                required_scope,
            )
        generated = {
            "wrong-audience": (
                "stram-negative-audience-" + secrets.token_hex(4),
                secrets.token_urlsafe(36),
                "https://invalid.stram.example/mcp",
                str(args.tenant),
                86400,
            ),
            "wrong-tenant": (
                "stram-negative-tenant-" + secrets.token_hex(4),
                secrets.token_urlsafe(36),
                str(args.resource),
                "invalid-tenant",
                86400,
            ),
            "expired": (
                "stram-negative-expired-" + secrets.token_hex(4),
                secrets.token_urlsafe(36),
                str(args.resource),
                str(args.tenant),
                1,
            ),
        }
        try:
            for client_id, client_secret, resource, tenant, lifespan in generated.values():
                _request(
                    admin_client,
                    "POST",
                    f"{args.admin_loopback}/admin/realms/{args.realm}/clients",
                    json=_client_payload(
                        client_id, client_secret, resource, tenant, lifespan=lifespan
                    ),
                )
                created_clients.append(client_id)
            wrong_issuer_id = "stram-negative-issuer-" + secrets.token_hex(4)
            wrong_issuer_secret = secrets.token_urlsafe(36)
            _request(
                admin_client,
                "POST",
                f"{args.admin_loopback}/admin/realms",
                json={
                    "realm": temporary_realm,
                    "enabled": True,
                    "sslRequired": "external",
                    "defaultSignatureAlgorithm": "RS256",
                    "accessTokenLifespan": 86400,
                    "clients": [
                        _client_payload(
                            wrong_issuer_id,
                            wrong_issuer_secret,
                            str(args.resource),
                            str(args.tenant),
                        )
                    ],
                },
            )
            realm_created = True
            tokens = {
                case: _token(
                    public_client,
                    public_url,
                    str(args.realm),
                    values[0],
                    values[1],
                )
                for case, values in generated.items()
            }
            tokens["wrong-issuer"] = _token(
                public_client,
                public_url,
                temporary_realm,
                wrong_issuer_id,
                wrong_issuer_secret,
            )
            jwks = _request(
                public_client,
                "GET",
                f"{public_url}/realms/{temporary_realm}/protocol/openid-connect/certs",
            ).json()
            expired_claims = jwt.decode(
                tokens["expired"], options={"verify_signature": False, "verify_aud": False}
            )
            expiry = expired_claims.get("exp")
            if not isinstance(expiry, int) or expiry > int(time.time()) + 10:
                raise RuntimeError(
                    "temporary expired-token client did not apply its one-second lifespan"
                )
            while int(time.time()) <= expiry:
                time.sleep(0.25)
            for case, token in tokens.items():
                _write(output / f"{case}.jwt", token + "\n")
            _write(
                output / "wrong-issuer.jwks.json",
                json.dumps(jwks, sort_keys=True, indent=2) + "\n",
            )
        finally:
            for client_id in created_clients:
                found = _request(
                    admin_client,
                    "GET",
                    f"{args.admin_loopback}/admin/realms/{args.realm}/clients",
                    params={"clientId": client_id},
                ).json()
                if isinstance(found, list) and found and isinstance(found[0].get("id"), str):
                    _request(
                        admin_client,
                        "DELETE",
                        f"{args.admin_loopback}/admin/realms/{args.realm}/clients/{found[0]['id']}",
                    )
            if realm_created:
                _request(
                    admin_client,
                    "DELETE",
                    f"{args.admin_loopback}/admin/realms/{temporary_realm}",
                )
    print("PASS: temporary Keycloak negative clients/realm removed; signed JWT evidence retained")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, httpx2.HTTPError, jwt.PyJWTError) as error:
        raise SystemExit(f"FAIL: negative-token evidence - {error}") from None
