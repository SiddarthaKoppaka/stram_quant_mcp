"""Every tool must reach the Data Plane only through the reviewed read-only routes."""

from __future__ import annotations

import pytest

from stram_quant_mcp.http_backend import HttpDataBackend
from tests.support.fake_data_plane import FakeDataPlane

READ_ROUTES = {
    ("GET", "/v1/catalog"),
    ("GET", "/v1/indicators"),
    ("GET", "/v1/policies"),
    ("GET", "/v1/freshness"),
    ("GET", "/v1/openapi.json"),
    ("POST", "/v1/series/query"),
    ("POST", "/v1/derived/query"),
    ("POST", "/v1/panel/query"),
    ("POST", "/v1/conditional-returns/query"),
    ("POST", "/token"),
}


@pytest.mark.asyncio
async def test_every_tool_layer_read_uses_only_reviewed_read_routes(
    backend: HttpDataBackend, data_plane: FakeDataPlane
) -> None:
    await backend.call(
        "quant_get_aligned_panel",
        {
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "columns": [{"name": "spy", "series_key": "SPY"}],
            "availability": "ex_post",
        },
        "read-only",
        20.0,
    )
    await backend.call("quant_list_series", {"limit": 1}, "read-only-2", 10.0)

    used = {(request.method, request.url.path) for request in data_plane.requests}
    assert used <= READ_ROUTES
    assert not {method for method, _ in used} & {"PUT", "PATCH", "DELETE"}
