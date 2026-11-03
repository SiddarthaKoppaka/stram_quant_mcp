"""Idempotently provision the second MCP's Keycloak scopes and clients in the shared realm.

Additive only: it creates or reconciles ``stram-qual-claims``, the ``qual:*`` scopes and the
``stram-qual-*`` clients. It never reads, updates or deletes Quant clients or scopes. Client
secrets are created once as owner-only files, are never printed, and tokens are never logged.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import jwt

CLAIMS_SCOPE = "stram-qual-claims"
QUAL_SCOPES = ("qual:discover", "qual:read", "qual:analytics", "qual:text")
# Least privilege: qual:text is requestable only by the dedicated text client.
CLIENTS: dict[str, tuple[str, ...]] = {
    "stram-qual-smoke": ("qual:discover", "qual:read", "qual:analytics"),
    "stram-qual-text": ("qual:discover", "qual:text"),
    "stram-qual-discovery-only": ("qual:discover",),
}
_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_SCOPE_DESCRIPTIONS = {
    "qual:discover": "Discover Qual MCP metadata",
    "qual:read": "Read bounded Qual MCP data",
    "qual:analytics": "Run bounded read-only Qual analytics",
    "qual:text": "Read Qual text content (never granted by default)",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-loopback", default="http://127.0.0.1:9080")
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--qual-resource", required=True)
    parser.add_argument("--quant-resource", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--bootstrap-password-file", type=Path, required=True)
    parser.add_argument("--secret-dir", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="plan only; make no change")
    parser.add_argument("--verify", action="store_true", help="also check issued token claims")
    return parser


def _owner_only_secret(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise RuntimeError("secret file is accessible beyond its owner")
    value = path.read_text().strip()
    if not _SECRET_PATTERN.fullmatch(value):
        raise RuntimeError("client secret file must contain 32-128 URL-safe base64 characters")
    return value


def load_or_create_secret(path: Path, *, create: bool) -> str | None:
    """Return the stored secret, creating it once (owner-only) when permitted."""

    if path.exists():
        return _owner_only_secret(path)
    if not create:
        return None
    value = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    return value


def _request(
    client: httpx2.Client,
    method: str,
    url: str,
    *,
    ok: tuple[int, ...] = (200, 201, 204),
    **kw: Any,
) -> httpx2.Response:
    response = client.request(method, url, **kw)
    if response.status_code not in ok:
        raise RuntimeError(f"Keycloak operation returned HTTP {response.status_code}")
    return response


def scope_representation(
    name: str, *, include_in_token: bool, mappers: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": _SCOPE_DESCRIPTIONS.get(name, "Qual MCP audience and tenant"),
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true" if include_in_token else "false",
            "display.on.consent.screen": "false",
        },
        "protocolMappers": mappers,
    }


def claims_mappers(qual_resource: str, tenant: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "stram-qual-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": qual_resource,
                "access.token.claim": "true",
                "id.token.claim": "false",
                "introspection.token.claim": "true",
            },
        },
        {
            "name": "stram-qual-tenant",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "tenant_id",
                "claim.value": tenant,
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "false",
                "introspection.token.claim": "true",
            },
        },
    ]


def client_representation(client_id: str, secret: str) -> dict[str, Any]:
    return {
        "clientId": client_id,
        "name": f"Stram Qual MCP {client_id.removeprefix('stram-qual-')} client",
        "enabled": True,
        "clientAuthenticatorType": "client-secret",
        "secret": secret,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "publicClient": False,
        "frontchannelLogout": False,
        "fullScopeAllowed": False,
        "defaultClientScopes": [CLAIMS_SCOPE],
        "optionalClientScopes": list(CLIENTS[client_id]),
        "attributes": {
            "client.session.idle.timeout": "300",
            "client.session.max.lifespan": "900",
            "oauth2.device.authorization.grant.enabled": "false",
            "oidc.ciba.grant.enabled": "false",
            "use.refresh.tokens": "false",
        },
    }


class Provisioner:
    def __init__(
        self,
        admin: httpx2.Client,
        base: str,
        realm: str,
        qual_resource: str,
        tenant: str,
        *,
        dry_run: bool,
    ) -> None:
        self._admin = admin
        self._realm_url = f"{base}/admin/realms/{realm}"
        self._qual_resource = qual_resource
        self._tenant = tenant
        self._dry_run = dry_run
        self.actions: list[str] = []

    def _do(self, description: str, method: str, path: str, **kw: Any) -> httpx2.Response | None:
        self.actions.append(description)
        if self._dry_run:
            return None
        return _request(self._admin, method, f"{self._realm_url}{path}", **kw)

    def _get_json(self, path: str, **kw: Any) -> Any:
        return _request(self._admin, "GET", f"{self._realm_url}{path}", **kw).json()

    def ensure_scopes(self) -> dict[str, str]:
        existing = {s["name"]: s for s in self._get_json("/client-scopes")}
        wanted = {
            CLAIMS_SCOPE: scope_representation(
                CLAIMS_SCOPE,
                include_in_token=False,
                mappers=claims_mappers(self._qual_resource, self._tenant),
            )
        }
        wanted |= {
            n: scope_representation(n, include_in_token=True, mappers=[]) for n in QUAL_SCOPES
        }
        for name, rep in wanted.items():
            if name not in existing:
                self._do(f"create client scope {name}", "POST", "/client-scopes", json=rep)
            elif name == CLAIMS_SCOPE:
                self._reconcile_mappers(existing[name]["id"], rep["protocolMappers"])
        if not self._dry_run:
            existing = {s["name"]: s for s in self._get_json("/client-scopes")}
        return {name: str(existing[name]["id"]) for name in wanted if name in existing}

    def _reconcile_mappers(self, scope_id: str, desired: list[dict[str, Any]]) -> None:
        current = {
            m["name"]: m
            for m in self._get_json(f"/client-scopes/{scope_id}/protocol-mappers/models")
        }
        for mapper in desired:
            found = current.get(mapper["name"])
            if found is None:
                self._do(
                    f"create mapper {mapper['name']}",
                    "POST",
                    f"/client-scopes/{scope_id}/protocol-mappers/models",
                    json=mapper,
                )
            elif found.get("config", {}) != {**found.get("config", {}), **mapper["config"]}:
                self._do(
                    f"update mapper {mapper['name']}",
                    "PUT",
                    f"/client-scopes/{scope_id}/protocol-mappers/models/{found['id']}",
                    json={**found, "config": {**found.get("config", {}), **mapper["config"]}},
                )

    def assert_no_realm_default_qual_scopes(self) -> None:
        for path in ("/default-default-client-scopes", "/default-optional-client-scopes"):
            leaked = [
                s["name"]
                for s in self._get_json(path)
                if str(s.get("name", "")).startswith(("qual:", "stram-qual"))
            ]
            if leaked:
                raise RuntimeError("a qual scope is a realm default; remove it before continuing")

    def ensure_client(self, client_id: str, secret: str | None, scope_ids: dict[str, str]) -> None:
        if not client_id.startswith("stram-qual-"):
            raise RuntimeError("refusing to touch a non-Qual client")
        found = self._get_json("/clients", params={"clientId": client_id})
        matches = [c for c in found if c.get("clientId") == client_id]
        if len(matches) > 1:
            raise RuntimeError(f"realm contains duplicate {client_id} clients")
        if not matches:
            self._do(
                f"create client {client_id}",
                "POST",
                "/clients",
                json=client_representation(client_id, secret or ""),
            )
            return
        uuid = str(matches[0]["id"])
        current_secret = self._get_json(f"/clients/{uuid}/client-secret").get("value")
        desired = client_representation(client_id, secret or "")
        if (
            secret is not None
            and (
                not isinstance(current_secret, str)
                or not secrets.compare_digest(current_secret, secret)
            )
        ) or any(
            matches[0].get(k) != desired[k]
            for k in (
                "enabled",
                "serviceAccountsEnabled",
                "directAccessGrantsEnabled",
                "standardFlowEnabled",
                "fullScopeAllowed",
            )
        ):
            self._do(
                f"update client {client_id}",
                "PUT",
                f"/clients/{uuid}",
                json={
                    **matches[0],
                    **{
                        k: v
                        for k, v in desired.items()
                        if k not in ("defaultClientScopes", "optionalClientScopes")
                    },
                },
            )
        self._reconcile_assignment(client_id, uuid, "default", {CLAIMS_SCOPE}, scope_ids)
        self._reconcile_assignment(client_id, uuid, "optional", set(CLIENTS[client_id]), scope_ids)

    def _reconcile_assignment(
        self, client_id: str, uuid: str, kind: str, wanted: set[str], scope_ids: dict[str, str]
    ) -> None:
        path = f"/clients/{uuid}/{kind}-client-scopes"
        current = {s["name"]: str(s["id"]) for s in self._get_json(path)}
        for name in sorted(wanted - current.keys()):
            self._do(
                f"assign {kind} scope {name} to {client_id}",
                "PUT",
                f"{path}/{scope_ids.get(name, 'pending')}",
            )
        for name in sorted(current.keys() - wanted):
            if name == CLAIMS_SCOPE or name.startswith("qual:"):
                self._do(
                    f"remove {kind} scope {name} from {client_id}",
                    "DELETE",
                    f"{path}/{current[name]}",
                )


def check_claims(
    claims: dict[str, Any], *, issuer: str, qual_resource: str, quant_resource: str, tenant: str
) -> list[str]:
    """Return failures for a decoded access-token payload (no token material in messages)."""

    failures: list[str] = []
    if claims.get("iss") != issuer:
        failures.append("iss mismatch")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else list(audience or [])
    if qual_resource not in audiences:
        failures.append("aud lacks the Qual resource")
    if quant_resource in audiences:
        failures.append("aud wrongly includes the Quant resource")
    if claims.get("tenant_id") != tenant:
        failures.append("tenant_id mismatch")
    for name in ("sub", "exp", "iat"):
        if not claims.get(name):
            failures.append(f"{name} missing")
    if not (claims.get("azp") or claims.get("client_id")):
        failures.append("azp/client_id missing")
    return failures


def _token_claims(
    public: httpx2.Client, token_url: str, client_id: str, secret: str, scope: str | None
) -> dict[str, Any] | None:
    """Return decoded claims, or None when Keycloak rejects the request (HTTP 400/401)."""

    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    response = public.post(token_url, data=data, auth=(client_id, secret))
    if response.status_code in (400, 401):
        return None
    if response.status_code != 200:
        raise RuntimeError(f"Keycloak operation returned HTTP {response.status_code}")
    token = response.json().get("access_token")
    if not isinstance(token, str) or jwt.get_unverified_header(token).get("alg") != "RS256":
        raise RuntimeError("token response was not an RS256 access token")
    claims: dict[str, Any] = jwt.decode(
        token, options={"verify_signature": False, "verify_aud": False}
    )
    return claims


def verify_clients(
    public: httpx2.Client,
    public_url: str,
    realm: str,
    creds: dict[str, str],
    *,
    qual_resource: str,
    quant_resource: str,
    tenant: str,
) -> list[str]:
    issuer = f"{public_url}/realms/{realm}"
    token_url = f"{issuer}/protocol/openid-connect/token"
    failures: list[str] = []
    for client_id, secret in creds.items():
        allowed = set(CLIENTS[client_id])
        default = _token_claims(public, token_url, client_id, secret, None)
        if default is None:
            failures.append(f"{client_id}: default token request was rejected")
            continue
        failures += [
            f"{client_id}: {f}"
            for f in check_claims(
                default,
                issuer=issuer,
                qual_resource=qual_resource,
                quant_resource=quant_resource,
                tenant=tenant,
            )
        ]
        if any(s.startswith("qual:") for s in str(default.get("scope", "")).split()):
            failures.append(f"{client_id}: a qual scope is granted by default")
        permitted = _token_claims(public, token_url, client_id, secret, " ".join(sorted(allowed)))
        granted = (
            {s for s in str(permitted.get("scope", "")).split() if s.startswith("qual:")}
            if permitted
            else set()
        )
        if granted != allowed:
            failures.append(f"{client_id}: permitted qual scopes were not all granted")
        for forbidden in sorted(set(QUAL_SCOPES) - allowed):
            probe = _token_claims(public, token_url, client_id, secret, forbidden)
            if probe is not None and forbidden in str(probe.get("scope", "")).split():
                failures.append(f"{client_id}: granted {forbidden} outside policy")
    return failures


def main() -> None:
    args = _parser().parse_args()
    os.umask(0o077)
    public_url = str(args.public_url).rstrip("/")
    if urlsplit(public_url).scheme != "https":
        raise SystemExit("BLOCKED: --public-url must be HTTPS")
    if (
        urlsplit(str(args.qual_resource)).scheme != "https"
        or args.qual_resource == args.quant_resource
    ):
        raise SystemExit(
            "BLOCKED: Qual resource must be HTTPS and distinct from the Quant resource"
        )
    secret_dir: Path = args.secret_dir
    secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_dir.chmod(0o700)
    creds = {}
    for client_id in CLIENTS:
        value = load_or_create_secret(secret_dir / f"{client_id}.secret", create=not args.dry_run)
        if value is not None:
            creds[client_id] = value
    host = urlsplit(public_url).netloc
    headers = {"Host": host, "X-Forwarded-Host": host, "X-Forwarded-Proto": "https"}
    verify: bool | str = str(args.ca_file) if args.ca_file else True
    with (
        httpx2.Client(timeout=15, verify=verify) as public,
        httpx2.Client(timeout=15, headers=headers) as admin,
    ):
        password = Path(args.bootstrap_password_file)
        if password.stat().st_mode & 0o077:
            raise SystemExit("BLOCKED: bootstrap password file must be owner-only")
        token = (
            _request(
                admin,
                "POST",
                f"{args.admin_loopback}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": "stram-bootstrap-admin",
                    "password": password.read_text().strip(),
                },
            )
            .json()
            .get("access_token")
        )
        if not isinstance(token, str) or not token:
            raise SystemExit("BLOCKED: Keycloak admin token response was invalid")
        admin.headers["Authorization"] = f"Bearer {token}"
        provisioner = Provisioner(
            admin,
            str(args.admin_loopback),
            args.realm,
            args.qual_resource,
            args.tenant,
            dry_run=args.dry_run,
        )
        provisioner.assert_no_realm_default_qual_scopes()
        scope_ids = provisioner.ensure_scopes()
        for client_id in CLIENTS:
            # A dry run never creates secrets, so it plans clients without one.
            provisioner.ensure_client(client_id, creds.get(client_id), scope_ids)
        for action in provisioner.actions:
            print(("PLAN: " if args.dry_run else "DONE: ") + action)
        if not provisioner.actions:
            print("PASS: Qual scopes and clients already match policy")
        if args.verify and not args.dry_run:
            failures = verify_clients(
                public,
                public_url,
                args.realm,
                creds,
                qual_resource=args.qual_resource,
                quant_resource=args.quant_resource,
                tenant=args.tenant,
            )
            for failure in failures:
                print(f"FAIL: {failure}")
            if failures:
                raise SystemExit(1)
            print("PASS: Qual token claims, audience isolation and qual:text policy verified")


if __name__ == "__main__":
    main()
