"""The clean view reshapes the raw result without changing any value, and raw is untouched."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from stram_quant_mcp.clean_models import (
    CleanCatalogData,
    CleanConditionalReturnsData,
    CleanDerivedData,
    CleanPanelData,
    CleanSeriesData,
)
from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_adapter import QuantToolAdapter

SERIES = {
    "series_keys": ["CPIAUCSL"],
    "kinds": ["economic"],
    "start_date": "2026-06-01",
    "end_date": "2026-07-31",
    "all_versions": True,
}
DERIVED = {
    "indicator_id": "treasury_10y_minus_2y",
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
}
PANEL = {
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
    "columns": [{"name": "spy", "series_key": "SPY"}, {"name": "ten", "series_key": "UST_10YEAR"}],
    "availability": "ex_post",
}
CONDITIONAL = {
    "target_symbol": "SPY",
    "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0.0}],
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
    "horizons": [1],
    "trigger": "while",
    "availability": "ex_post",
}
CASES = [
    ("quant_list_series", {"limit": 20}),
    ("quant_get_series", SERIES),
    ("quant_get_derived", DERIVED),
    ("quant_get_aligned_panel", PANEL),
    ("quant_get_conditional_returns", CONDITIONAL),
]


def _run(adapter: QuantToolAdapter, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = adapter.execute(tool, arguments, "request").model_dump(mode="json")
    return result


@pytest.mark.parametrize(("tool", "arguments"), CASES)
def test_raw_view_is_unchanged_when_requested_explicitly(
    tool_adapter: QuantToolAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    default = _run(tool_adapter, tool, arguments)
    explicit = _run(tool_adapter, tool, {**arguments, "view": "raw"})

    assert json.dumps(default, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert "view" not in default["data"]


@pytest.mark.parametrize(("tool", "arguments"), CASES)
def test_clean_view_keeps_envelope_and_warnings(
    tool_adapter: QuantToolAdapter, tool: str, arguments: dict[str, Any]
) -> None:
    raw = _run(tool_adapter, tool, arguments)
    clean = _run(tool_adapter, tool, {**arguments, "view": "clean"})

    assert clean["data"]["view"] == "clean"
    assert {key: value for key, value in clean.items() if key not in {"data", "request_id"}} == {
        key: value for key, value in raw.items() if key not in {"data", "request_id"}
    }


def test_clean_catalog_lists_every_raw_entry(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_list_series", {"limit": 20})["data"]["entries"]
    clean = CleanCatalogData.model_validate(
        _run(tool_adapter, "quant_list_series", {"limit": 20, "view": "clean"})["data"]
    )

    keys = [dict(zip(clean.columns, row, strict=True)) for row in clean.rows]
    assert [row["series_key"] for row in keys] == [entry["series_key"] for entry in raw]
    assert [row["record_count"] for row in keys] == [entry["record_count"] for entry in raw]
    assert clean.count == len(raw)
    assert sum(clean.by_dataset.values()) == len(raw)


def test_clean_series_values_match_raw_rows(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_series", SERIES)["data"]
    clean = CleanSeriesData.model_validate(
        _run(tool_adapter, "quant_get_series", {**SERIES, "view": "clean"})["data"]
    )

    flattened = [
        (series.series_key, row[0], row[1], row[2])
        for series in clean.series
        for row in series.rows
    ]
    expected = [
        (row["series_key"], row["observed_on"], row["value"], row["known_since"])
        for row in raw["rows"]
    ]
    assert sorted(flattened, key=str) == sorted(expected, key=str)
    assert (clean.count, clean.truncated, clean.all_versions) == (
        raw["count"],
        raw["truncated"],
        raw["all_versions"],
    )


def test_clean_derived_values_are_exactly_equal(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_derived", DERIVED)["data"]
    clean = CleanDerivedData.model_validate(
        _run(tool_adapter, "quant_get_derived", {**DERIVED, "view": "clean"})["data"]
    )

    by_event = {row["event_id"]: Decimal(row["value"]) for row in raw["rows"]}
    rows = [row for group in clean.groups for row in group.rows]
    assert len(rows) == len(by_event)
    for _date, value, _known, event_id, _flags in rows:
        assert Decimal(str(value)) == by_event[str(event_id)]
    assert rows[0][1] == "0.4"


def test_clean_panel_cells_match_raw_cells(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_aligned_panel", PANEL)["data"]
    clean = CleanPanelData.model_validate(
        _run(tool_adapter, "quant_get_aligned_panel", {**PANEL, "view": "clean"})["data"]
    )

    assert clean.columns == ("observed_on", *raw["columns"])
    for raw_row, clean_row in zip(raw["rows"], clean.rows, strict=True):
        assert clean_row[0] == raw_row["observed_on"]
        for name, value in zip(raw["columns"], clean_row[1:], strict=True):
            cell = raw_row["cells"][name]
            assert value == (cell["value"] if cell else None)
    ten = next(column for column in clean.column_details if column.name == "ten")
    assert ten.missing_cells == 1


def test_clean_conditional_returns_keep_summary_and_baseline(
    tool_adapter: QuantToolAdapter,
) -> None:
    raw = _run(tool_adapter, "quant_get_conditional_returns", CONDITIONAL)["data"]
    clean = CleanConditionalReturnsData.model_validate(
        _run(tool_adapter, "quant_get_conditional_returns", {**CONDITIONAL, "view": "clean"})[
            "data"
        ]
    )

    for horizon, summary, baseline in zip(
        clean.horizons, raw["summary"], raw["baseline"], strict=True
    ):
        assert (horizon.horizon, horizon.mean, horizon.hit_rate) == (
            summary["horizon"],
            summary["mean"],
            summary["hit_rate"],
        )
        assert (horizon.baseline_mean, horizon.baseline_observations) == (
            baseline["mean"],
            baseline["observations"],
        )
    assert clean.event_count == raw["event_count"] == len(clean.events)
    assert clean.events[0][:3] == (
        raw["events"][0]["event_date"],
        raw["events"][0]["entry_date"],
        raw["events"][0]["entry_close"],
    )
    assert clean.notes == tuple(raw["notes"])


def test_an_unknown_view_is_rejected(tool_adapter: QuantToolAdapter) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_series", {**SERIES, "view": "pretty"}, "request")


def test_tools_without_a_clean_view_reject_the_option(tool_adapter: QuantToolAdapter) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_freshness", {"view": "clean"}, "request")
