"""Black-box harness for the behavior suite.

Every test in ``tests/parity`` talks to the service only through HTTP and the audit journal, so the
assertions describe the public contract and survive any change of framework internals.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from stram_quant_mcp.fastmcp_auth import StrictVerifier
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.tool_registry import TOOL_SPECS

ISSUER = "https://idp.test/realms/stram"
RESOURCE = "https://mcp.test/mcp"
HOST = "mcp.test"
ALLOWED_ORIGIN = "https://client.example"
TENANT = "local-stram"
ALL_SCOPES = tuple(sorted({spec.scope for spec in TOOL_SPECS}))
TOKEN_LIFETIME_SECONDS = 300
ACCEPT = "application/json, text/event-stream"
LAKE = Path(__file__).parents[1] / "fixtures/lake"


def build_service(settings: AppSettings, minter: TokenMinter, audit_key: bytes) -> ASGIApp:
    """Build the service, trusting only the minter's key."""

    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())
    built = create_fastmcp_application(settings, token_verifier=verifier, audit_key=audit_key)
    return built.app


class TokenMinter:
    """Signs access tokens with a throwaway key so the real verifier can be exercised."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def public_key_pem(self) -> bytes:
        """Return the PEM public key that verifies this minter's tokens."""

        return self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def mint(
        self,
        scope: str,
        *,
        overrides: Mapping[str, object] | None = None,
        drop: Iterable[str] = (),
        signing_key: Any = None,
    ) -> str:
        """Return a valid RS256 token, optionally corrupted by overriding or dropping claims."""

        now = int(time.time())
        claims: dict[str, object] = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "researcher-1",
            "client_id": "quant-client",
            "tenant_id": TENANT,
            "scope": scope,
            "iat": now - 1,
            "exp": now + TOKEN_LIFETIME_SECONDS,
        }
        claims.update(overrides or {})
        for name in drop:
            claims.pop(name, None)
        return jwt.encode(claims, signing_key or self.key, algorithm="RS256")


@dataclass(frozen=True)
class Reply:
    """The parts of an HTTP reply that parity tests assert on."""

    status: int
    headers: Mapping[str, str]
    body: Any

    @property
    def result(self) -> dict[str, Any]:
        """The JSON-RPC result object."""

        return dict(self.body["result"])


class ParityClient:
    """Sends JSON-RPC requests to the service under test."""

    def __init__(self, client: TestClient, minter: TokenMinter, audit_db: Path) -> None:
        self._client = client
        self.minter = minter
        self.audit_db = audit_db
        self._next_id = 0

    def rpc(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        """POST one JSON-RPC request and return the reply."""

        self._next_id += 1
        request_headers = {"accept": ACCEPT, "content-type": "application/json"}
        if token:
            request_headers["authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        response = self._client.post("/mcp", json=body, headers=request_headers)
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = None
        return Reply(response.status_code, dict(response.headers), parsed)

    def post_raw(self, body: bytes, token: str | None) -> Reply:
        """POST raw bytes to the MCP endpoint (used for size-limit checks)."""

        headers = {"accept": ACCEPT, "content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        response = self._client.post("/mcp", content=body, headers=headers)
        return Reply(response.status_code, dict(response.headers), None)

    def call_tool(self, name: str, arguments: Mapping[str, object], token: str | None) -> Reply:
        """Invoke ``tools/call``."""

        return self.rpc("tools/call", {"name": name, "arguments": dict(arguments)}, token=token)

    def get(self, path: str) -> Any:
        """GET a path (used for discovery documents)."""

        return self._client.get(path)

    def audit_outcomes(self) -> list[str]:
        """Return the audit outcomes recorded so far, oldest first."""

        with sqlite3.connect(self.audit_db) as connection:
            rows = connection.execute("SELECT record_json FROM audit_records ORDER BY sequence")
            return [str(json.loads(row[0])["outcome"]) for row in rows]

    def audit_text(self) -> str:
        """Return every audit record as one string, for leak checks."""

        with sqlite3.connect(self.audit_db) as connection:
            rows = connection.execute("SELECT record_json FROM audit_records ORDER BY sequence")
            return "\n".join(str(row[0]) for row in rows)


def parity_settings(tmp_path: Path) -> AppSettings:
    """Return loopback-safe settings that match the documented production shape."""

    return AppSettings(
        parquet_root=LAKE.resolve(),
        audit_db_path=tmp_path / "audit.sqlite3",
        audit_key_path=tmp_path / "audit.key",
        issuer_url=ISSUER,
        resource_url=RESOURCE,
        jwks_url="https://idp.test/jwks",
        tenant_id=TENANT,
        allowed_hosts=(HOST,),
        allowed_origins=(ALLOWED_ORIGIN,),
        image_digest="sha256:parity",
    )
