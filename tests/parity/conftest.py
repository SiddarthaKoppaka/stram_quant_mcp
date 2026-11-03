"""Fixtures that run each parity test once per registered implementation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.parity.harness import (
    HOST,
    IMPLEMENTATIONS,
    ParityClient,
    TokenMinter,
    parity_settings,
)

AUDIT_KEY = b"k" * 32


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def implementation(request: pytest.FixtureRequest) -> str:
    """Name of the implementation under test."""

    return str(request.param)


def _open_client(name: str, tmp_path: Path) -> Iterator[ParityClient]:
    minter = TokenMinter()
    settings = parity_settings(tmp_path)
    app = IMPLEMENTATIONS[name](settings, minter, AUDIT_KEY)
    with TestClient(app, base_url=f"https://{HOST}") as client:
        yield ParityClient(client, minter, settings.audit_db_path)


@pytest.fixture
def parity(implementation: str, tmp_path: Path) -> Iterator[ParityClient]:
    """A client for a fresh instance of the implementation under test."""

    yield from _open_client(implementation, tmp_path)


@pytest.fixture
def fastmcp_parity(tmp_path: Path) -> Iterator[ParityClient]:
    """A client for the FastMCP implementation; skips until it is registered (plan Task 4)."""

    if "fastmcp" not in IMPLEMENTATIONS:
        pytest.skip("The FastMCP implementation is added in migration plan Task 4.")
    yield from _open_client("fastmcp", tmp_path)
