"""Run credential-safe authenticated acceptance checks against an authorized staging endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx2
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import JsonValue

EXPECTED_SCOPES = frozenset({"quant:discover", "quant:read", "quant:lineage", "quant:analytics"})
EXPECTED_CLASSES = frozenset({"discovery", "read", "lineage", "analytics"})
NEGATIVE_CASES = ("wrong-issuer", "wrong-audience", "wrong-tenant", "expired")


@dataclass(frozen=True, slots=True)
class Configuration:
    endpoint: str
    issuer: str
    resource: str
    tenant: str
    client_id: str
    client_secret_file: Path
    discovery_client_id: str
    discovery_client_secret_file: Path
    calls_file: Path
    negative_token_dir: Path | None
    ca_file: Path | None
    require_complete: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--client-id", default="stram-quant-smoke")
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument("--discovery-client-id", default="stram-quant-discovery-only")
    parser.add_argument("--discovery-client-secret-file", type=Path, required=True)
    parser.add_argument("--calls-file", type=Path, required=True)
    parser.add_argument("--negative-token-dir", type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def _configuration(args: argparse.Namespace) -> Configuration:
    return Configuration(
        endpoint=str(args.endpoint),
        issuer=str(args.issuer).rstrip("/"),
        resource=str(args.resource),
        tenant=str(args.tenant),
        client_id=str(args.client_id),
        client_secret_file=cast(Path, args.client_secret_file),
        discovery_client_id=str(args.discovery_client_id),
        discovery_client_secret_file=cast(Path, args.discovery_client_secret_file),
        calls_file=cast(Path, args.calls_file),
        negative_token_dir=cast(Path | None, args.negative_token_dir),
        ca_file=cast(Path | None, args.ca_file),
        require_complete=bool(args.require_complete),
    )


def _secret(path: Path) -> str:
    value = path.read_text().strip()
    if not value or path.stat().st_mode & 0o077:
        raise RuntimeError("client secret file is empty or accessible beyond its owner")
    return value


def _ssl_context(ca_file: Path | None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)


def _safe_status(label: str, status: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"{status}: {label}{suffix}")


async def _json_get(client: httpx2.AsyncClient, url: str) -> dict[str, JsonValue]:
    response = await client.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"HTTPS metadata request returned {response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("HTTPS metadata response is not an object")
    return cast(dict[str, JsonValue], value)


async def _token(
    client: httpx2.AsyncClient,
    token_endpoint: str,
    client_id: str,
    secret_file: Path,
    scopes: str,
) -> str:
    response = await client.post(
        token_endpoint,
        data={"grant_type": "client_credentials", "scope": scopes},
        auth=(client_id, _secret(secret_file)),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"token request returned {response.status_code}")
    value = response.json()
    token = value.get("access_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("token response omitted access_token")
    return token


def _claims(token: str) -> dict[str, object]:
    value = jwt.decode(
        token,
        options={"verify_signature": False, "verify_aud": False},
        algorithms=["RS256"],
    )
    return cast(dict[str, object], value)


def _audiences(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(cast(list[str], value))
    return frozenset()


def _verify_positive_claims(config: Configuration, token: str) -> None:
    claims = _claims(token)
    now = int(time.time())
    if claims.get("iss") != config.issuer:
        raise RuntimeError("token issuer does not exactly match configuration")
    if config.resource not in _audiences(claims.get("aud")):
        raise RuntimeError("token audience does not include the exact MCP resource")
    if claims.get("tenant_id") != config.tenant:
        raise RuntimeError("token tenant does not exactly match configuration")
    if not isinstance(claims.get("iat"), int) or not isinstance(claims.get("exp"), int):
        raise RuntimeError("token is missing integer iat/exp")
    if cast(int, claims["iat"]) > now + 30 or cast(int, claims["exp"]) <= now:
        raise RuntimeError("token time claims are not currently valid")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise RuntimeError("token subject is missing")
    if not (claims.get("client_id") or claims.get("azp")):
        raise RuntimeError("token client identity is missing")
    raw_scope = claims.get("scope", claims.get("scp"))
    scopes = frozenset(raw_scope.split()) if isinstance(raw_scope, str) else frozenset()
    if not EXPECTED_SCOPES.issubset(scopes):
        raise RuntimeError("token does not contain all explicitly requested Quant scopes")


def _verify_negative_signature(
    config: Configuration,
    token: str,
    jwks: dict[str, JsonValue],
    case: str,
) -> dict[str, object]:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise RuntimeError(f"{case} token does not use a keyed RS256 signature")
    key_set = jwt.PyJWKSet.from_dict(cast(dict[str, object], jwks))
    signing_key = next((key.key for key in key_set.keys if key.key_id == header["kid"]), None)
    if signing_key is None:
        raise RuntimeError(f"{case} token is not signed by a current Keycloak key")
    options = {
        "verify_iss": case != "wrong-issuer",
        "verify_aud": case != "wrong-audience",
        "verify_exp": case != "expired",
        "require": ["exp", "iat", "iss", "aud", "sub"],
    }
    value = jwt.decode(
        token,
        key=signing_key,
        algorithms=["RS256"],
        issuer=config.issuer,
        audience=config.resource,
        options=options,
    )
    return cast(dict[str, object], value)


def _calls(path: Path) -> list[tuple[str, str, dict[str, JsonValue]]]:
    value = json.loads(path.read_text())
    rows = value.get("calls") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("calls file must contain a calls array")
    calls: list[tuple[str, str, dict[str, JsonValue]]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("each staging call must be an object")
        category, tool, arguments = row.get("class"), row.get("tool"), row.get("arguments")
        if (
            not isinstance(category, str)
            or not isinstance(tool, str)
            or not isinstance(arguments, dict)
        ):
            raise RuntimeError("staging call requires string class/tool and object arguments")
        encoded = json.dumps(arguments, sort_keys=True)
        if "REPLACE_WITH_" in encoded:
            raise RuntimeError("calls file still contains replacement markers")
        calls.append((category, tool, cast(dict[str, JsonValue], arguments)))
    if frozenset(category for category, _, _ in calls) != EXPECTED_CLASSES:
        raise RuntimeError("calls file must cover discovery, read, lineage and analytics exactly")
    return calls


async def _transport_denials(
    client: httpx2.AsyncClient, config: Configuration, valid_token: str
) -> None:
    cases = (
        ("missing token", {}),
        ("bad signature", {"Authorization": "Bearer invalid.invalid.invalid"}),
        (
            "rejected Host",
            {"Authorization": f"Bearer {valid_token}", "Host": "attacker.invalid"},
        ),
        (
            "rejected Origin",
            {
                "Authorization": f"Bearer {valid_token}",
                "Origin": "https://attacker.invalid",
            },
        ),
    )
    expected = {
        "missing token": {401},
        "bad signature": {401},
        "rejected Host": {421},
        "rejected Origin": {403},
    }
    for label, headers in cases:
        response = await client.post(config.endpoint, headers=headers, json={})
        if response.status_code not in expected[label]:
            raise RuntimeError(f"{label} returned unexpected status {response.status_code}")
        _safe_status(label, "PASS", f"HTTP {response.status_code}")


async def _official_client(
    client: httpx2.AsyncClient,
    endpoint: str,
    token: str,
    calls: list[tuple[str, str, dict[str, JsonValue]]],
) -> None:
    client.headers["Authorization"] = f"Bearer {token}"
    async with (
        streamable_http_client(
            endpoint,
            http_client=client,
            terminate_on_close=False,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        if initialized.server_info.name != "stram-quant" or len(listed.tools) != 10:
            raise RuntimeError("official MCP initialization or exact ten-tool contract failed")
        _safe_status("official MCP initialize and list exactly 10 tools", "PASS")
        known = {tool.name for tool in listed.tools}
        for category, tool, arguments in calls:
            if tool not in known:
                raise RuntimeError(f"configured {category} tool is not advertised")
            result = await session.call_tool(tool, arguments)
            if result.is_error or result.structured_content is None:
                raise RuntimeError(f"representative {category} tool call failed")
            _safe_status(f"real-lake {category} call", "PASS", tool)


async def _insufficient_scope(
    config: Configuration,
    ssl_context: ssl.SSLContext,
    token_endpoint: str,
) -> None:
    async with httpx2.AsyncClient(timeout=10, verify=ssl_context) as token_client:
        token = await _token(
            token_client,
            token_endpoint,
            config.discovery_client_id,
            config.discovery_client_secret_file,
            "quant:discover",
        )
    async with (
        httpx2.AsyncClient(
            timeout=10,
            verify=ssl_context,
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client,
        streamable_http_client(
            config.endpoint,
            http_client=http_client,
            terminate_on_close=False,
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        result = await session.call_tool("quant_get_freshness", {})
    if not result.is_error:
        raise RuntimeError("discovery-only token unexpectedly called a read tool")
    _safe_status("insufficient scope", "PASS")


async def _provided_negative_tokens(
    config: Configuration,
    client: httpx2.AsyncClient,
    jwks: dict[str, JsonValue],
) -> bool:
    if config.negative_token_dir is None:
        for case in NEGATIVE_CASES:
            _safe_status(case, "NOT VERIFIED", "no operator-provided signed token")
        return False
    complete = True
    now = int(time.time())
    for case in NEGATIVE_CASES:
        path = config.negative_token_dir / f"{case}.jwt"
        if not path.is_file():
            complete = False
            _safe_status(case, "NOT VERIFIED", "signed token file is absent")
            continue
        token = path.read_text().strip()
        claims = _verify_negative_signature(config, token, jwks, case)
        wrong = {
            "wrong-issuer": claims.get("iss") != config.issuer,
            "wrong-audience": config.resource not in _audiences(claims.get("aud")),
            "wrong-tenant": claims.get("tenant_id") != config.tenant,
            "expired": isinstance(claims.get("exp"), int) and cast(int, claims["exp"]) < now,
        }[case]
        if not wrong:
            raise RuntimeError(f"{case} token does not contain the claimed negative condition")
        response = await client.post(
            config.endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        if response.status_code != 401:
            raise RuntimeError(f"{case} token returned HTTP {response.status_code}")
        _safe_status(case, "PASS", "HTTP 401")
    return complete


async def _run(config: Configuration) -> int:
    ssl_context = _ssl_context(config.ca_file)
    discovery_url = f"{config.issuer}/.well-known/openid-configuration"
    async with httpx2.AsyncClient(timeout=10, verify=ssl_context) as client:
        discovery = await _json_get(client, discovery_url)
        if discovery.get("issuer") != config.issuer:
            raise RuntimeError("OIDC discovery issuer does not exactly match configuration")
        token_endpoint = discovery.get("token_endpoint")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(token_endpoint, str) or not isinstance(jwks_uri, str):
            raise RuntimeError("OIDC discovery omitted token_endpoint or jwks_uri")
        jwks = await _json_get(client, jwks_uri)
        if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise RuntimeError("JWKS has no keys")
        _safe_status("trusted HTTPS discovery and non-empty JWKS", "PASS")
        token = await _token(
            client,
            token_endpoint,
            config.client_id,
            config.client_secret_file,
            " ".join(sorted(EXPECTED_SCOPES)),
        )
        _verify_positive_claims(config, token)
        _safe_status("Keycloak token claims, audience, tenant and scopes", "PASS")
        await _transport_denials(client, config, token)
        negative_complete = await _provided_negative_tokens(config, client, jwks)

    await _insufficient_scope(config, ssl_context, token_endpoint)
    calls = _calls(config.calls_file)
    async with httpx2.AsyncClient(timeout=30, verify=ssl_context) as mcp_client:
        await _official_client(mcp_client, config.endpoint, token, calls)
    if config.require_complete and not negative_complete:
        _safe_status(
            "staging acceptance", "BLOCKED", "signed negative-token evidence is incomplete"
        )
        return 2
    _safe_status("staging acceptance", "PASS" if negative_complete else "NOT VERIFIED")
    return 0 if negative_complete or not config.require_complete else 2


def main() -> None:
    config = _configuration(_parser().parse_args())
    try:
        raise SystemExit(asyncio.run(_run(config)))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, jwt.PyJWTError) as error:
        raise SystemExit(f"FAIL: staging acceptance - {error}") from None


if __name__ == "__main__":
    main()
