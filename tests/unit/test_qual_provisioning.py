from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tools import provision_qual_clients as qual

ISSUER = "https://siddu.tailc5da4c.ts.net/realms/stram-staging"
QUANT = "https://siddu.tailc5da4c.ts.net/mcp"
QUAL = "https://siddu.tailc5da4c.ts.net:8443/mcp"
BASE = "http://127.0.0.1:9080"
ADMIN = f"{BASE}/admin/realms/stram-staging"


class FakeKeycloak:
    """Minimal in-memory Keycloak admin/token API, including the existing Quant client."""

    def __init__(self) -> None:
        self.scopes: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.assign: dict[str, dict[str, dict[str, str]]] = {}
        self.writes: list[tuple[str, str]] = []
        self.realm_defaults: list[dict[str, str]] = []
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.clients["uuid-quant"] = {
            "id": "uuid-quant",
            "clientId": "stram-quant-smoke",
            "secret": "quant-secret-unchanged",  # pragma: allowlist secret
        }
        self.assign["uuid-quant"] = {"default": {}, "optional": {}}
        self.next_id = 0

    def _id(self) -> str:
        self.next_id += 1
        return f"id-{self.next_id}"

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url, method = str(request.url), request.method
        if method != "GET" and "/token" not in url:
            self.writes.append((method, url))
        if url.endswith("/realms/master/protocol/openid-connect/token"):
            return httpx2.Response(200, json={"access_token": "admin"})
        if url.endswith("/realms/stram-staging/protocol/openid-connect/token"):
            return self._token(request)
        if not url.startswith(ADMIN):
            return httpx2.Response(404)
        path = url[len(ADMIN) :].split("?")[0]
        if path in {"/default-default-client-scopes", "/default-optional-client-scopes"}:
            return httpx2.Response(200, json=self.realm_defaults)
        if path == "/client-scopes":
            if method == "POST":
                body = json.loads(request.content)
                self.scopes[body["name"]] = {**body, "id": self._id()}
                return httpx2.Response(201)
            return httpx2.Response(
                200, json=[{"id": s["id"], "name": n} for n, s in self.scopes.items()]
            )
        if m := re.fullmatch(r"/client-scopes/([^/]+)/protocol-mappers/models", path):
            scope = next(s for s in self.scopes.values() if s["id"] == m[1])
            return httpx2.Response(
                200, json=[{**mp, "id": mp["name"]} for mp in scope["protocolMappers"]]
            )
        if path == "/clients":
            if method == "POST":
                body = json.loads(request.content)
                uuid = self._id()
                self.clients[uuid] = {**body, "id": uuid}
                self.assign[uuid] = {
                    "default": {n: self.scopes[n]["id"] for n in body["defaultClientScopes"]},
                    "optional": {n: self.scopes[n]["id"] for n in body["optionalClientScopes"]},
                }
                return httpx2.Response(201)
            wanted = request.url.params.get("clientId")
            return httpx2.Response(
                200, json=[c for c in self.clients.values() if c["clientId"] == wanted]
            )
        if m := re.fullmatch(r"/clients/([^/]+)/client-secret", path):
            return httpx2.Response(200, json={"value": self.clients[m[1]]["secret"]})
        if m := re.fullmatch(r"/clients/([^/]+)/(default|optional)-client-scopes", path):
            return httpx2.Response(
                200, json=[{"id": i, "name": n} for n, i in self.assign[m[1]][m[2]].items()]
            )
        if m := re.fullmatch(r"/clients/([^/]+)/(default|optional)-client-scopes/([^/]+)", path):
            name = next(n for n, s in self.scopes.items() if s["id"] == m[3])
            (
                self.assign[m[1]][m[2]].__setitem__(name, m[3])
                if method == "PUT"
                else self.assign[m[1]][m[2]].pop(name)
            )
            return httpx2.Response(204)
        if m := re.fullmatch(r"/clients/([^/]+)", path) and method == "PUT":
            uuid = path.rsplit("/", 1)[1]
            self.clients[uuid].update(json.loads(request.content))
            return httpx2.Response(204)
        return httpx2.Response(404)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        import base64
        from urllib.parse import parse_qs

        cid, _, secret = (
            base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
        )
        client = next((c for c in self.clients.values() if c["clientId"] == cid), None)
        if client is None or client["secret"] != secret:
            return httpx2.Response(401)
        uuid = client["id"]
        requested = set(parse_qs(request.content.decode()).get("scope", [""])[0].split())
        permitted = set(self.assign[uuid]["optional"])
        if requested - permitted:
            return httpx2.Response(400, json={"error": "invalid_scope"})
        granted = sorted(requested)
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": QUAL,
            "sub": f"sa-{cid}",
            "azp": cid,
            "tenant_id": "local-stram",
            "iat": now,
            "exp": now + 300,
            "scope": " ".join(granted),
        }
        return httpx2.Response(
            200, json={"access_token": jwt.encode(claims, self.key, "RS256", headers={"kid": "k"})}
        )


def _run(fake: FakeKeycloak, secret_dir: Path, *, dry_run: bool = False) -> qual.Provisioner:
    secrets_by_client = {
        c: qual.load_or_create_secret(secret_dir / f"{c}.secret", create=True) for c in qual.CLIENTS
    }
    with httpx2.Client(transport=httpx2.MockTransport(fake)) as admin:
        provisioner = qual.Provisioner(
            admin, BASE, "stram-staging", QUAL, "local-stram", dry_run=dry_run
        )
        provisioner.assert_no_realm_default_qual_scopes()
        ids = provisioner.ensure_scopes()
        for client_id, secret in secrets_by_client.items():
            provisioner.ensure_client(client_id, str(secret), ids)
    return provisioner


def test_provisioning_is_additive_idempotent_and_never_touches_quant(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    _run(fake, tmp_path)
    assert {"stram-qual-claims", *qual.QUAL_SCOPES} <= fake.scopes.keys()
    assert fake.clients["uuid-quant"]["secret"] == "quant-secret-unchanged"  # noqa: S105  # pragma: allowlist secret
    assert all("uuid-quant" not in url for _, url in fake.writes)
    writes_after_first = len(fake.writes)
    again = _run(fake, tmp_path)
    assert again.actions == []
    assert len(fake.writes) == writes_after_first


def test_qual_text_is_optional_on_the_text_client_only(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    _run(fake, tmp_path)
    by_name = {c["clientId"]: fake.assign[c["id"]] for c in fake.clients.values()}
    for client_id, assignment in by_name.items():
        if not client_id.startswith("stram-qual-"):
            continue
        assert set(assignment["default"]) == {"stram-qual-claims"}
        assert ("qual:text" in assignment["optional"]) is (client_id == "stram-qual-text")
    audience = fake.scopes["stram-qual-claims"]["protocolMappers"][0]["config"][
        "included.custom.audience"
    ]
    assert audience == QUAL != QUANT
    assert all(
        fake.scopes[s]["attributes"]["include.in.token.scope"] == "true" for s in qual.QUAL_SCOPES
    )


def test_drift_is_repaired_and_a_leaked_default_text_scope_is_removed(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    _run(fake, tmp_path)
    smoke = next(c["id"] for c in fake.clients.values() if c["clientId"] == "stram-qual-smoke")
    fake.assign[smoke]["default"]["qual:text"] = fake.scopes["qual:text"]["id"]
    repaired = _run(fake, tmp_path)
    assert "qual:text" not in fake.assign[smoke]["default"]
    assert any("remove default scope qual:text" in a for a in repaired.actions)


def test_realm_default_qual_scope_blocks_provisioning(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    fake.realm_defaults = [{"id": "x", "name": "qual:text"}]
    with pytest.raises(RuntimeError, match="realm default"):
        _run(fake, tmp_path)
    assert fake.writes == []


def test_dry_run_makes_no_change(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    plan = _run(fake, tmp_path, dry_run=True)
    assert plan.actions and fake.writes == [] and not fake.scopes


def test_dry_run_plans_every_client_without_creating_secrets() -> None:
    fake = FakeKeycloak()
    with httpx2.Client(transport=httpx2.MockTransport(fake)) as admin:
        provisioner = qual.Provisioner(
            admin, BASE, "stram-staging", QUAL, "local-stram", dry_run=True
        )
        ids = provisioner.ensure_scopes()
        for client_id in qual.CLIENTS:
            provisioner.ensure_client(client_id, None, ids)
    for client_id in qual.CLIENTS:
        assert f"create client {client_id}" in provisioner.actions
    assert fake.writes == []


def test_verification_passes_for_correct_tokens_and_rejects_wrong_ones(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    _run(fake, tmp_path)
    creds = {c: (tmp_path / f"{c}.secret").read_text() for c in qual.CLIENTS}
    with httpx2.Client(transport=httpx2.MockTransport(fake)) as public:
        ok = qual.verify_clients(
            public,
            "https://siddu.tailc5da4c.ts.net",
            "stram-staging",
            creds,
            qual_resource=QUAL,
            quant_resource=QUANT,
            tenant="local-stram",
        )
        assert ok == []
        # Policy drift: smoke client also holds qual:text -> verification fails closed.
        smoke = next(c["id"] for c in fake.clients.values() if c["clientId"] == "stram-qual-smoke")
        fake.assign[smoke]["optional"]["qual:text"] = fake.scopes["qual:text"]["id"]
        bad = qual.verify_clients(
            public,
            "https://siddu.tailc5da4c.ts.net",
            "stram-staging",
            creds,
            qual_resource=QUAL,
            quant_resource=QUANT,
            tenant="local-stram",
        )
        assert any("granted qual:text outside policy" in f for f in bad)


def _good_claims() -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": QUAL,
        "sub": "s",
        "azp": "c",
        "tenant_id": "local-stram",
        "iat": 1,
        "exp": 2,
    }


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"aud": QUANT}, "aud lacks"),
        ({"aud": [QUAL, QUANT]}, "wrongly includes"),
        ({"iss": "https://evil.example/realms/x"}, "iss mismatch"),
        ({"tenant_id": "other"}, "tenant_id mismatch"),
        ({"sub": None}, "sub missing"),
        ({"exp": None}, "exp missing"),
        ({"iat": None}, "iat missing"),
        ({"azp": None}, "azp/client_id missing"),
    ],
)
def test_claim_checks_reject_bad_tokens(mutation: dict[str, Any], expected: str) -> None:
    assert (
        qual.check_claims(
            _good_claims(),
            issuer=ISSUER,
            qual_resource=QUAL,
            quant_resource=QUANT,
            tenant="local-stram",
        )
        == []
    )
    failures = qual.check_claims(
        {**_good_claims(), **mutation},
        issuer=ISSUER,
        qual_resource=QUAL,
        quant_resource=QUANT,
        tenant="local-stram",
    )
    assert any(expected in f for f in failures)


def test_secrets_are_owner_only_stable_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "s.secret"
    first = qual.load_or_create_secret(path, create=True)
    assert path.stat().st_mode & 0o777 == 0o600
    assert qual.load_or_create_secret(path, create=True) == first
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="beyond its owner"):
        qual.load_or_create_secret(path, create=False)
    path.chmod(0o600)
    path.write_text("short")
    with pytest.raises(RuntimeError, match="URL-safe"):
        qual.load_or_create_secret(path, create=False)
    assert qual.load_or_create_secret(tmp_path / "missing", create=False) is None
