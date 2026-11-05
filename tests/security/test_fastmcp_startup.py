"""The FastMCP service must not become ready, and must not leak a worker, when startup fails."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from stram_quant_mcp.errors import ToolUnavailableError
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from stram_quant_mcp.settings import AppSettings
from tests.parity.harness import HOST, TENANT
from tests.support.fake_data_plane import FakeDataPlane
from tests.support.settings import make_settings


def _loopback_settings(tmp_path: Path) -> AppSettings:
    return make_settings(
        tmp_path,
        issuer_url="http://127.0.0.1/realms/stram",
        resource_url="http://127.0.0.1:9/mcp",
        jwks_url="http://127.0.0.1:1/jwks",
        tenant_id=TENANT,
        allowed_hosts=(HOST,),
    )


def test_an_unreachable_jwks_endpoint_stops_startup_and_closes_the_backend(tmp_path: Path) -> None:
    built = create_fastmcp_application(
        _loopback_settings(tmp_path), audit_key=b"k" * 32, data_transport=FakeDataPlane()
    )

    with pytest.raises(RuntimeError, match="JWKS"), TestClient(built.app):
        pass

    assert built.runtime.ready is False
    assert built.runtime.backend.ready is False


def test_an_unreachable_data_plane_stops_startup(tmp_path: Path) -> None:
    plane = FakeDataPlane()
    plane.script(lambda _request: httpx.Response(503, json={}))
    built = create_fastmcp_application(
        _loopback_settings(tmp_path), audit_key=b"k" * 32, data_transport=plane
    )

    with pytest.raises(ToolUnavailableError) as raised, TestClient(built.app):
        pass

    assert raised.value.retryable is True
    assert built.runtime.ready is False
