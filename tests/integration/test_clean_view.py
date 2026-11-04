"""The clean view is lossless: every raw row can be rebuilt from it, and raw is untouched."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from pydantic import JsonValue

from stram_quant_mcp.clean_views import LIMIT_FILLED, NO_EVENTS, NO_ROWS
from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_adapter import QuantToolAdapter
from stram_quant_mcp.tool_models import (
    CleanCatalogData,
    CleanConditionalReturnsData,
    CleanDerivedData,
    CleanDerivedGroup,
    CleanPanelData,
    CleanSeries,
    CleanSeriesData,
)

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
STATISTICS = ("observations", "mean", "median", "hit_rate", "minimum", "maximum")
# Parsed JSON payloads are indexed freely in assertions; the clean models validate their shape.
Payload = dict[str, Any]
Arguments = dict[str, JsonValue]


def _run(adapter: QuantToolAdapter, tool: str, arguments: Arguments) -> Payload:
    result: Payload = adapter.execute(tool, arguments, "request").model_dump(mode="json")
    return result


def _clean(adapter: QuantToolAdapter, tool: str, arguments: Arguments) -> Payload:
    data: Payload = _run(adapter, tool, {**arguments, "view": "clean"})["data"]
    return data


def _canonical(rows: list[Payload]) -> list[str]:
    return sorted(json.dumps(row, sort_keys=True) for row in rows)


def _rebuild_groups(groups: Sequence[CleanSeries | CleanDerivedGroup]) -> list[Payload]:
    return [
        {
            **group.model_dump(mode="json", exclude={"columns", "rows"}),
            **dict(zip(group.columns, row, strict=True)),
        }
        for group in groups
        for row in group.rows
    ]


@pytest.mark.parametrize(("tool", "arguments"), CASES)
def test_raw_view_is_unchanged_when_requested_explicitly(
    tool_adapter: QuantToolAdapter, tool: str, arguments: Arguments
) -> None:
    default = _run(tool_adapter, tool, arguments)
    explicit = _run(tool_adapter, tool, {**arguments, "view": "raw"})

    assert json.dumps(default, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert "view" not in default["data"]


@pytest.mark.parametrize(("tool", "arguments"), CASES)
def test_clean_view_keeps_envelope_and_warnings(
    tool_adapter: QuantToolAdapter, tool: str, arguments: Arguments
) -> None:
    raw = _run(tool_adapter, tool, arguments)
    clean = _run(tool_adapter, tool, {**arguments, "view": "clean"})

    assert clean["data"]["view"] == "clean"
    assert {key: value for key, value in clean.items() if key not in {"data", "request_id"}} == {
        key: value for key, value in raw.items() if key not in {"data", "request_id"}
    }


def test_clean_catalog_rebuilds_every_raw_entry(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_list_series", {"limit": 20})["data"]["entries"]
    clean = CleanCatalogData.model_validate(
        _clean(tool_adapter, "quant_list_series", {"limit": 20})
    )

    rebuilt = [dict(zip(clean.columns, row, strict=True)) for row in clean.rows]
    for entry in rebuilt:
        entry.pop("frequency_normalized")

    assert rebuilt == raw
    assert clean.by_dataset == dict(Counter(entry["dataset"] for entry in raw))
    assert clean.by_provider == dict(Counter(entry["provider"] for entry in raw))


def test_a_catalog_that_fills_the_limit_says_more_may_exist(
    tool_adapter: QuantToolAdapter,
) -> None:
    clean = CleanCatalogData.model_validate(_clean(tool_adapter, "quant_list_series", {"limit": 1}))

    assert (clean.count, clean.limit_filled) == (1, True)
    assert clean.explanation == LIMIT_FILLED


def test_a_catalog_below_the_limit_is_not_called_filled(tool_adapter: QuantToolAdapter) -> None:
    clean = CleanCatalogData.model_validate(
        _clean(tool_adapter, "quant_list_series", {"limit": 500})
    )

    assert clean.count < clean.limit
    assert (clean.limit_filled, clean.explanation) == (False, None)


def test_clean_series_rebuilds_every_raw_row(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_series", SERIES)["data"]
    clean = CleanSeriesData.model_validate(_clean(tool_adapter, "quant_get_series", SERIES))

    rebuilt = _rebuild_groups(clean.series)

    assert _canonical(rebuilt) == _canonical(raw["rows"])
    assert clean.availability.model_dump(mode="json") == raw["availability"]


def test_clean_derived_rebuilds_every_raw_row_with_exact_values(
    tool_adapter: QuantToolAdapter,
) -> None:
    raw = _run(tool_adapter, "quant_get_derived", DERIVED)["data"]
    clean = CleanDerivedData.model_validate(_clean(tool_adapter, "quant_get_derived", DERIVED))

    rebuilt = _rebuild_groups(clean.groups)
    clean_values = {row["event_id"]: Decimal(row.pop("value")) for row in rebuilt}
    raw_values = {row["event_id"]: Decimal(row.pop("value")) for row in raw["rows"]}

    assert clean_values == raw_values
    assert _canonical(rebuilt) == _canonical(raw["rows"])


def test_clean_derived_values_lose_only_trailing_zeros(tool_adapter: QuantToolAdapter) -> None:
    clean = CleanDerivedData.model_validate(_clean(tool_adapter, "quant_get_derived", DERIVED))

    first = dict(zip(clean.groups[0].columns, clean.groups[0].rows[0], strict=True))

    assert first["value"] == "0.4"


def test_clean_panel_rebuilds_every_raw_cell(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_aligned_panel", PANEL)["data"]
    clean = CleanPanelData.model_validate(_clean(tool_adapter, "quant_get_aligned_panel", PANEL))

    cells = [dict(zip(clean.cell_columns, cell, strict=True)) for cell in clean.cells]
    rebuilt = {
        (cell["observed_on"], cell["column"]): {
            "value": cell["value"],
            "observed_on": cell["cell_observed_on"],
            "known_since": cell["known_since"],
            "filled": cell["filled"],
            "stale_days": cell["stale_days"],
            "availability": cell["availability"],
        }
        for cell in cells
    }
    expected = {
        (row["observed_on"], name): cell
        for row in raw["rows"]
        for name, cell in row["cells"].items()
        if cell is not None
    }

    assert rebuilt == expected
    assert [row[0] for row in clean.rows] == [row["observed_on"] for row in raw["rows"]]


def test_clean_panel_tells_absent_cells_from_null_values(tool_adapter: QuantToolAdapter) -> None:
    clean = CleanPanelData.model_validate(_clean(tool_adapter, "quant_get_aligned_panel", PANEL))

    ten = next(column for column in clean.column_details if column.name == "ten")

    assert (ten.absent_cells, ten.null_values) == (1, 0)
    assert ("2026-10-02", "ten") not in {(cell[0], cell[1]) for cell in clean.cells}


def test_clean_conditional_returns_keep_every_statistic(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_conditional_returns", CONDITIONAL)["data"]
    clean = CleanConditionalReturnsData.model_validate(
        _clean(tool_adapter, "quant_get_conditional_returns", CONDITIONAL)
    )

    outcomes = [[getattr(h, name) for name in STATISTICS] for h in clean.horizons]
    baselines = [[getattr(h, f"baseline_{name}") for name in STATISTICS] for h in clean.horizons]

    assert outcomes == [[item[name] for name in STATISTICS] for item in raw["summary"]]
    assert baselines == [[item[name] for name in STATISTICS] for item in raw["baseline"]]
    assert clean.notes == tuple(raw["notes"])


def test_clean_conditional_returns_rebuild_every_event(tool_adapter: QuantToolAdapter) -> None:
    raw = _run(tool_adapter, "quant_get_conditional_returns", CONDITIONAL)["data"]
    clean = CleanConditionalReturnsData.model_validate(
        _clean(tool_adapter, "quant_get_conditional_returns", CONDITIONAL)
    )

    events = [
        {
            **dict(zip(clean.event_columns, event, strict=True)),
            "conditions": [
                dict(zip(clean.condition_columns, row, strict=True))
                for row in dict(zip(clean.event_columns, event, strict=True))["conditions"]
            ],
        }
        for event in clean.events
    ]

    assert events == raw["events"]


def test_an_empty_window_explains_itself(tool_adapter: QuantToolAdapter) -> None:
    empty = {**SERIES, "start_date": "2001-01-01", "end_date": "2001-01-31"}

    clean = CleanSeriesData.model_validate(_clean(tool_adapter, "quant_get_series", empty))

    assert clean.series == ()
    assert clean.explanation == NO_ROWS


def test_a_study_without_events_explains_itself(tool_adapter: QuantToolAdapter) -> None:
    never = {
        **CONDITIONAL,
        "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 1000.0}],
    }

    clean = CleanConditionalReturnsData.model_validate(
        _clean(tool_adapter, "quant_get_conditional_returns", never)
    )

    assert clean.event_count == 0
    assert clean.explanation == NO_EVENTS


def test_an_unknown_view_is_rejected(tool_adapter: QuantToolAdapter) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_series", {**SERIES, "view": "pretty"}, "request")


def test_tools_without_a_clean_view_reject_the_option(tool_adapter: QuantToolAdapter) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_freshness", {"view": "clean"}, "request")
