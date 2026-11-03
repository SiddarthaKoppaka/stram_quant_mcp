from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from tools import staging_acceptance

ORIGIN = "https://siddu.tailc5da4c.ts.net"
RESOURCE = f"{ORIGIN}/mcp"
ISSUER = f"{ORIGIN}/realms/stram-staging"
PRM_URL = f"{ORIGIN}/.well-known/oauth-protected-resource/mcp"
ASM_URL = f"{ORIGIN}/.well-known/oauth-authorization-server/realms/stram-staging"


def _config() -> staging_acceptance.Configuration:
    return staging_acceptance.Configuration(
        endpoint=RESOURCE,
        issuer=ISSUER,
        resource=RESOURCE,
        tenant="local-stram",
        client_id="c",
        client_secret_file=Path("x"),
        discovery_client_id="d",
        discovery_client_secret_file=Path("y"),
        calls_file=Path("z"),
        negative_token_dir=None,
        ca_file=None,
        require_complete=False,
    )


def _check(**overrides: object) -> None:
    state = {
        "status": 401,
        "challenge": f'Bearer error="invalid_token", resource_metadata="{PRM_URL}"',
        "prm": {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
        },
        "asm": {"issuer": ISSUER},
        "prm_status": 200,
    } | overrides

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx2.Response(
                state["status"], headers={"www-authenticate": state["challenge"]}
            )  # type: ignore[arg-type]
        if url == PRM_URL:
            return httpx2.Response(state["prm_status"], json=state["prm"])  # type: ignore[arg-type]
        if url == ASM_URL:
            return httpx2.Response(200, json=state["asm"])
        return httpx2.Response(404)

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            await staging_acceptance._verify_resource_discovery(client, _config())

    asyncio.run(run())


def test_valid_discovery_chain_passes() -> None:
    _check()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"challenge": 'Bearer error="invalid_token"'}, "resource_metadata"),
        ({"challenge": 'Bearer resource_metadata="https://evil.example/x"'}, "resource_metadata"),
        ({"status": 200}, "resource_metadata"),
        ({"prm_status": 404}, "404"),
        (
            {
                "prm": {
                    "resource": ORIGIN + "/other",
                    "authorization_servers": [ISSUER],
                    "bearer_methods_supported": ["header"],
                }
            },
            "resource does not match",
        ),
        (
            {
                "prm": {
                    "resource": RESOURCE,
                    "authorization_servers": ["https://evil.example"],
                    "bearer_methods_supported": ["header"],
                }
            },
            "unexpected authorization server",
        ),
        (
            {
                "prm": {
                    "resource": RESOURCE,
                    "authorization_servers": [ISSUER],
                    "bearer_methods_supported": ["body"],
                }
            },
            "non-header",
        ),
        ({"asm": {"issuer": "https://evil.example"}}, "issuer does not match"),
    ],
)
def test_broken_discovery_chain_fails_closed(override: dict[str, object], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _check(**override)
