"""Production entrypoint; configuration is environment-only and fail-closed."""

from __future__ import annotations

import os

import uvicorn
from starlette.types import ASGIApp

from stram_quant_mcp.server import create_application
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.telemetry import configure_telemetry


def create_from_env() -> ASGIApp:
    settings = AppSettings.from_env()
    return create_application(settings, telemetry=configure_telemetry(settings)).app


def main() -> None:
    host = os.environ.get("STRAM_MCP_BIND_HOST", "127.0.0.1")
    # Wildcard bind is container-only; Host/Origin allowlists remain enforced in the app.
    if host not in {"127.0.0.1", "0.0.0.0"}:  # noqa: S104
        raise ValueError("STRAM_MCP_BIND_HOST must be 127.0.0.1 or 0.0.0.0")
    port = int(os.environ.get("STRAM_MCP_BIND_PORT", "8000"))
    if port < 1 or port > 65535:
        raise ValueError("STRAM_MCP_BIND_PORT must be 1 to 65535")
    uvicorn.run(
        "stram_quant_mcp.app:create_from_env",
        factory=True,
        host=host,
        port=port,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
