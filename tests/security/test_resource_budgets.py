from __future__ import annotations

import pytest
from pydantic import ValidationError

from stram_quant_mcp.tool_models import ConditionalReturnsInput, PanelInput, SeriesInput


@pytest.mark.parametrize(
    "model,payload",
    [
        (
            SeriesInput,
            {
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "series_keys": [f"S{i}" for i in range(11)],
            },
        ),
        (
            PanelInput,
            {
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "columns": [{"name": f"c{i}", "series_key": "SPY"} for i in range(9)],
            },
        ),
        (
            ConditionalReturnsInput,
            {
                "target_symbol": "SPY",
                "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0}],
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "limit": 201,
            },
        ),
    ],
)
def test_wrapper_limits_are_stricly_below_upstream_maxima(
    model: type[SeriesInput] | type[PanelInput] | type[ConditionalReturnsInput],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
