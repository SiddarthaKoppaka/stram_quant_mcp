"""The FastMCP service must not become ready, and must not leak a worker, when startup fails."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from stram_quant_mcp.settings import AppSettings
from tests.parity.harness import HOST, LAKE, TENANT


def _loopback_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        parquet_root=LAKE.resolve(),
        audit_db_path=tmp_path / "audit.sqlite3",
        audit_key_path=tmp_path / "audit.key",
        issuer_url="http://127.0.0.1/realms/stram",
        resource_url="http://127.0.0.1:9/mcp",
        jwks_url="http://127.0.0.1:1/jwks",
        tenant_id=TENANT,
        allowed_hosts=(HOST,),
        allow_insecure_http=True,
    )


def test_an_unreachable_jwks_endpoint_stops_startup_and_the_worker(tmp_path: Path) -> None:
    built = create_fastmcp_application(_loopback_settings(tmp_path), audit_key=b"k" * 32)

    with pytest.raises(RuntimeError, match="JWKS"), TestClient(built.app):
        pass

    assert built.runtime.ready is False
    assert built.runtime.worker.ready is False
