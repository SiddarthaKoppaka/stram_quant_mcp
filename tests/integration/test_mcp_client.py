from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from stram_quant_mcp.auth import CallableKeyResolver, JWTTokenVerifier, JWTVerifierConfig
from stram_quant_mcp.server import create_application
from stram_quant_mcp.settings import AppSettings


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_official_client_lists_and_calls_real_streamable_http_tools(tmp_path) -> None:
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    settings = AppSettings(
        parquet_root=Path(__file__).parents[1] / "fixtures/lake",
        audit_db_path=tmp_path / "audit.sqlite3",
        audit_key_path=tmp_path / "audit.key",
        issuer_url="http://127.0.0.1/issuer",
        resource_url=endpoint,
        jwks_url="http://127.0.0.1/jwks",
        tenant_id="stram",
        allowed_hosts=(f"127.0.0.1:{port}",),
        allow_insecure_http=True,
        image_digest="sha256:test",
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    compact = jwt.encode(
        {
            "iss": "http://127.0.0.1/issuer",
            "aud": endpoint,
            "sub": "researcher-1",
            "client_id": "quant-client",
            "tenant_id": "stram",
            "scope": "quant:discover quant:read quant:lineage quant:analytics",
            "iat": now - 1,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
    )
    verifier = JWTTokenVerifier(
        JWTVerifierConfig(
            issuer="http://127.0.0.1/issuer",
            audience=endpoint,
            tenant_id="stram",
            leeway_seconds=0,
        ),
        CallableKeyResolver(lambda _token: private_key.public_key()),
    )
    built = create_application(
        settings,
        token_verifier=verifier,
        audit_key=b"k" * 32,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            built.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {compact}"},
                timeout=5,
            ) as http_client,
            streamable_http_client(
                endpoint,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("quant_list_series", {"limit": 1})

        assert initialized.server_info.name == "stram-quant"
        assert len(listed.tools) == 10
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in listed.tools)
        assert result.is_error is False
        assert result.structured_content is not None
        assert len(result.structured_content["data"]["entries"]) == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
