from __future__ import annotations

import pytest

from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_models import ConditionalReturnsEnvelope, PanelEnvelope
from tests.support.sync_adapter import SyncAdapter


def test_aligned_panel_delegates_calendar_and_gap_semantics(
    tool_adapter: SyncAdapter,
) -> None:
    result = tool_adapter.execute(
        "quant_get_aligned_panel",
        {
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "columns": [
                {"name": "spy", "series_key": "SPY"},
                {"name": "ten", "series_key": "UST_10YEAR"},
            ],
            "availability": "ex_post",
        },
        "panel",
    )
    assert isinstance(result, PanelEnvelope)
    assert result.data.columns == ("spy", "ten")
    assert [row.observed_on.isoformat() for row in result.data.rows] == [
        "2026-10-01",
        "2026-10-02",
    ]
    first_spy = result.data.rows[0].cells["spy"]
    first_ten = result.data.rows[0].cells["ten"]
    assert first_spy is not None and first_spy.value == 100.0
    assert first_ten is not None and first_ten.value == 4.5
    assert result.data.rows[1].cells["ten"] is None
    assert result.data.availability.hindsight_only is True


def test_conditional_returns_preserve_signal_timing_lineage_and_baseline(
    tool_adapter: SyncAdapter,
) -> None:
    result = tool_adapter.execute(
        "quant_get_conditional_returns",
        {
            "target_symbol": "SPY",
            "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0.0}],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "horizons": [1],
            "trigger": "while",
            "availability": "ex_post",
        },
        "conditional",
    )
    assert isinstance(result, ConditionalReturnsEnvelope)
    assert result.data.event_count == 2
    assert result.data.baseline[0].horizon == 1
    first = result.data.events[0]
    assert first.signal_available_at is not None
    assert first.signal_cutoff_at is not None
    assert first.entry_at is not None
    assert first.signal_available_at <= first.signal_cutoff_at < first.entry_at
    assert first.conditions[0].source_event_id
    assert result.data.availability.hindsight_only is True


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (
            "quant_get_aligned_panel",
            {
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "columns": [
                    {"name": "same", "series_key": "SPY"},
                    {"name": "same", "series_key": "UST_10YEAR"},
                ],
            },
        ),
        (
            "quant_get_aligned_panel",
            {
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "columns": [{"name": "spy", "series_key": "SPY"}],
                "fill": "forward",
                "max_staleness_days": 0,
            },
        ),
        (
            "quant_get_conditional_returns",
            {
                "target_symbol": "SPY",
                "conditions": [{"series_key": "UST_10YEAR", "operator": "gt"}],
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        ),
        (
            "quant_get_conditional_returns",
            {
                "target_symbol": "SPY",
                "conditions": [
                    {"series_key": "UST_10YEAR", "operator": "rising", "threshold": 1.0}
                ],
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        ),
        (
            "quant_get_conditional_returns",
            {
                "target_symbol": "SPY",
                "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0.0}],
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "horizons": [1, 1],
            },
        ),
    ],
)
def test_analytics_bounds_fail_before_data_access(
    tool_adapter: SyncAdapter, tool: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute(tool, arguments, "invalid")
