"""Fixtures that start a fresh service for each behavior test."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.parity.harness import HOST, ParityClient, TokenMinter, build_service, parity_settings
from tests.support.fake_data_plane import FakeDataPlane

AUDIT_KEY = b"k" * 32


@pytest.fixture
def data_plane() -> FakeDataPlane:
    """The in-process Data Plane the service under test talks to."""

    return FakeDataPlane()


@pytest.fixture
def parity(tmp_path: Path, data_plane: FakeDataPlane) -> Iterator[ParityClient]:
    """A client for a fresh instance of the service."""

    minter = TokenMinter()
    settings = parity_settings(tmp_path)
    app = build_service(settings, minter, AUDIT_KEY, data_plane)
    with TestClient(app, base_url=f"https://{HOST}") as client:
        yield ParityClient(client, minter, settings.audit_db_path)
