"""quant_get_series: the order input, the 5,000-row limit and the default request."""

from __future__ import annotations

import json

import pytest

from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_models import SeriesEnvelope
from tests.support.fake_data_plane import FakeDataPlane
from tests.support.sync_adapter import SyncAdapter

SERIES_PATH = "/v1/series/query"
SPY = {
    "series_keys": ["SPY"],
    "kinds": ["market_bar"],
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
}
# The body the Data Plane received for a default call before `order` existed (Data Interface 1.12).
DEFAULT_BODY = (
    b'{"start_date":"2026-10-01","end_date":"2026-10-02","series_keys":["SPY"],'
    b'"indicator_ids":[],"kinds":["market_bar"],"provider":null,"as_of":null,'
    b'"all_versions":false,"limit":100,"availability":"strict"}'
)


def _series(adapter: SyncAdapter, arguments: dict[str, object]) -> SeriesEnvelope:
    result = adapter.execute("quant_get_series", arguments, "series")
    assert isinstance(result, SeriesEnvelope)
    return result


def _body(plane: FakeDataPlane) -> dict[str, object]:
    return json.loads(plane.calls_to(SERIES_PATH)[-1].content)


def test_a_default_call_sends_the_request_it_sent_before_order_existed(
    tool_adapter: SyncAdapter, data_plane: FakeDataPlane
) -> None:
    result = _series(tool_adapter, SPY)

    assert data_plane.calls_to(SERIES_PATH)[-1].content == DEFAULT_BODY
    assert [row.observed_on.isoformat() for row in result.data.rows] == ["2026-10-01", "2026-10-02"]


def test_an_explicit_asc_is_the_default_call(
    tool_adapter: SyncAdapter, data_plane: FakeDataPlane
) -> None:
    _series(tool_adapter, {**SPY, "order": "asc"})

    assert data_plane.calls_to(SERIES_PATH)[-1].content == DEFAULT_BODY


def test_desc_is_passed_to_the_data_interface_and_lists_newest_first(
    tool_adapter: SyncAdapter, data_plane: FakeDataPlane
) -> None:
    result = _series(tool_adapter, {**SPY, "order": "desc"})

    assert _body(data_plane)["order"] == "desc"
    assert [row.observed_on.isoformat() for row in result.data.rows] == ["2026-10-02", "2026-10-01"]


def test_a_cutting_limit_keeps_the_oldest_rows_by_default_and_the_newest_with_desc(
    tool_adapter: SyncAdapter,
) -> None:
    oldest = _series(tool_adapter, {**SPY, "limit": 1})
    newest = _series(tool_adapter, {**SPY, "limit": 1, "order": "desc"})

    assert oldest.data.truncated is True and newest.data.truncated is True
    assert [row.observed_on.isoformat() for row in oldest.data.rows] == ["2026-10-01"]
    assert [row.observed_on.isoformat() for row in newest.data.rows] == ["2026-10-02"]


def test_the_clean_view_keeps_the_requested_order(tool_adapter: SyncAdapter) -> None:
    result = tool_adapter.execute(
        "quant_get_series", {**SPY, "order": "desc", "view": "clean"}, "clean"
    )
    dumped = result.model_dump(mode="json")["data"]["series"][0]
    column = dumped["columns"].index("observed_on")

    assert [row[column] for row in dumped["rows"]] == ["2026-10-02", "2026-10-01"]


def test_the_limit_reaches_the_data_interface_maximum_and_no_further(
    tool_adapter: SyncAdapter, data_plane: FakeDataPlane
) -> None:
    _series(tool_adapter, {**SPY, "limit": 5000})
    assert _body(data_plane)["limit"] == 5000

    with pytest.raises(ToolInputError):
        _series(tool_adapter, {**SPY, "limit": 5001})


def test_the_derived_limit_stays_at_500(tool_adapter: SyncAdapter) -> None:
    arguments: dict[str, object] = {
        "indicator_id": "treasury_10y_minus_2y",
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
    }
    tool_adapter.execute("quant_get_derived", {**arguments, "limit": 500}, "ok")
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_derived", {**arguments, "limit": 501}, "too many")


@pytest.mark.parametrize("order", ["newest", "ASC", "", None])
def test_an_unknown_order_is_rejected(tool_adapter: SyncAdapter, order: object) -> None:
    with pytest.raises(ToolInputError):
        _series(tool_adapter, {**SPY, "order": order})
