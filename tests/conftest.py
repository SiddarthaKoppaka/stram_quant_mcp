from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from stram_quant_mcp.data_client import DataPlaneClient, DataPlaneSettings
from stram_quant_mcp.data_gateway import DataGateway
from stram_quant_mcp.http_backend import HttpDataBackend
from stram_quant_mcp.tool_adapter import QuantToolAdapter
from tests.support.fake_data_plane import DATA_URL, TOKEN_URL, FakeDataPlane
from tests.support.sync_adapter import SyncAdapter


@pytest.fixture
def secret_file(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "client.secret"
    path.write_text("test-client-secret")
    return path


@pytest.fixture
def data_plane() -> FakeDataPlane:
    return FakeDataPlane()


@pytest.fixture
def client(data_plane: FakeDataPlane, secret_file) -> DataPlaneClient:  # type: ignore[no-untyped-def]
    settings = DataPlaneSettings(
        base_url=DATA_URL,
        token_url=TOKEN_URL,
        client_id="stram-quant-mcp-test",
        client_secret_path=secret_file,
    )
    return DataPlaneClient(settings, transport=data_plane)


@pytest_asyncio.fixture
async def backend(client: DataPlaneClient) -> AsyncIterator[HttpDataBackend]:
    started = HttpDataBackend(client)
    await started.start()
    yield started
    await started.close(drain_seconds=1.0)


@pytest.fixture
def tool_adapter(client: DataPlaneClient) -> SyncAdapter:
    gateway = DataGateway(client)
    adapter = QuantToolAdapter(gateway, policy_version="test-policy", service_version="1.9.0")
    return SyncAdapter(adapter)
