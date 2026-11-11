"""The official MCP client against the real FastMCP server over real HTTP."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from stram_quant_mcp.fastmcp_auth import StrictVerifier
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from tests.parity.harness import TENANT, TokenMinter
from tests.support.fake_data_plane import FakeDataPlane
from tests.support.settings import make_settings

ISSUER = "http://127.0.0.1/issuer"
SERVER_START_ATTEMPTS = 100
ALL_SCOPES = "quant:discover quant:read quant:lineage quant:analytics"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_official_client_lists_and_calls_tools_on_the_fastmcp_server(tmp_path: Path) -> None:
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    settings = make_settings(
        tmp_path,
        issuer_url=ISSUER,
        resource_url=endpoint,
        jwks_url="http://127.0.0.1/jwks",
        tenant_id=TENANT,
        allowed_hosts=(f"127.0.0.1:{port}",),
    )
    minter = TokenMinter()
    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())
    built = create_fastmcp_application(
        settings,
        token_verifier=verifier,
        audit_key=b"k" * 32,
        data_transport=FakeDataPlane(),
    )
    token = minter.mint(
        ALL_SCOPES, overrides={"iss": ISSUER, "aud": endpoint, "client_id": "quant-client"}
    )
    server = uvicorn.Server(
        uvicorn.Config(built.app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(SERVER_START_ATTEMPTS):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        headers = {"Authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=5) as http_client,
            streamable_http_client(endpoint, http_client=http_client, terminate_on_close=False) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("quant_list_series", {"limit": 1})

        assert initialized.server_info.name == "stram-quant"
        assert len(listed.tools) == len(set(tool.name for tool in listed.tools)) == 11
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in listed.tools)
        assert result.is_error is False
        assert len((result.structured_content or {})["data"]["entries"]) == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)
