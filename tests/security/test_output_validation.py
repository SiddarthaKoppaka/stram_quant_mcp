"""Data Plane replies and tool envelopes are validated before they reach a client."""

from __future__ import annotations

import httpx
import pytest

from stram_quant_mcp import errors
from stram_quant_mcp.http_backend import HttpDataBackend
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME
from tests.support.fake_data_plane import FakeDataPlane

LEAK = "/internal/lake/path"


def _reply(plane: FakeDataPlane, path: str, content: bytes) -> None:
    plane.script(lambda r: httpx.Response(200, content=content) if r.url.path == path else None)


@pytest.mark.asyncio
async def test_a_reply_that_drifted_from_the_contract_is_a_named_non_retryable_failure(
    backend: HttpDataBackend, data_plane: FakeDataPlane
) -> None:
    _reply(data_plane, "/v1/catalog", b'[{"dataset": "market_bar", "unexpected": 1}]')

    with pytest.raises(errors.ToolUpstreamError) as raised:
        await backend.call("quant_list_series", {"limit": 1}, "req-1", 5.0)

    assert raised.value.retryable is False
    assert "reviewed contract" in raised.value.reason
    assert "incompatible version" in raised.value.reason


@pytest.mark.asyncio
async def test_a_non_json_reply_is_rejected_without_echoing_it(
    backend: HttpDataBackend, data_plane: FakeDataPlane
) -> None:
    _reply(data_plane, "/v1/freshness", f"<html>{LEAK}</html>".encode())

    with pytest.raises(errors.ToolUpstreamError) as raised:
        await backend.call("quant_get_freshness", {}, "req-1", 5.0)

    assert LEAK not in raised.value.caller_text


@pytest.mark.asyncio
async def test_unknown_tools_and_extra_arguments_never_reach_the_data_plane(
    backend: HttpDataBackend, data_plane: FakeDataPlane
) -> None:
    before = len(data_plane.requests)

    with pytest.raises(errors.ToolInputError):
        await backend.call("quant_drop_everything", {}, "req-1", 5.0)
    with pytest.raises(errors.ToolInputError) as raised:
        await backend.call("quant_list_series", {"limit": 1, "sql": "select 1"}, "req-1", 5.0)

    assert "sql" in raised.value.reason
    assert len(data_plane.requests) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(TOOLS_BY_NAME))
async def test_every_successful_envelope_round_trips_its_declared_output_model(
    backend: HttpDataBackend, tool: str
) -> None:
    examples: dict[str, dict[str, object]] = {
        "quant_list_indicator_subjects": {"indicator_id": "treasury_10y_minus_2y"},
        "quant_get_series": {
            "series_keys": ["SPY"],
            "kinds": ["market_bar"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
        },
        "quant_get_derived": {
            "indicator_id": "treasury_10y_minus_2y",
            "start_date": "2026-10-01",
            "end_date": "2026-10-01",
        },
        "quant_get_aligned_panel": {
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "columns": [{"name": "spy", "series_key": "SPY"}],
        },
        "quant_get_conditional_returns": {
            "target_symbol": "SPY",
            "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0.0}],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "horizons": [1],
        },
        "quant_get_latest_quotes": {"symbols": ["SPY"]},
    }
    if tool == "quant_get_lineage":
        derived = await backend.call("quant_get_derived", examples["quant_get_derived"], "seed", 5)
        event_id = derived["data"]["rows"][0]["event_id"]  # type: ignore[index]
        arguments: dict[str, object] = {"event_id": event_id}
    else:
        arguments = examples.get(tool, {})

    payload = await backend.call(tool, arguments, "round-trip", 20.0)

    validated = TOOLS_BY_NAME[tool].output_model.model_validate(payload)
    assert validated.model_dump(mode="json") == payload
