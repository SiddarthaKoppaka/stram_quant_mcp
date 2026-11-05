"""Pydantic input validation must name the field and rule, and never echo the submitted value."""

from __future__ import annotations

import pytest

from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_adapter import QuantToolAdapter
from stram_quant_mcp.tool_models import SeriesInput

MARKER = "seeded-marker-value"


def _describe(arguments: dict[str, object]) -> str:
    from pydantic import ValidationError

    from stram_quant_mcp.input_validation import describe_validation_error

    with pytest.raises(ValidationError) as raised:
        SeriesInput.model_validate(arguments)
    return describe_validation_error(raised.value)


def test_reason_names_the_field_and_the_broken_rule() -> None:
    reason = _describe({"series_keys": ["SPY"], "start_date": "2026-10-01", "end_date": "x"})

    assert "end_date" in reason
    assert "date" in reason.lower()


def test_reason_never_echoes_submitted_values() -> None:
    reason = _describe(
        {"series_keys": [MARKER], "start_date": MARKER, "end_date": MARKER, "limit": MARKER}
    )

    assert MARKER not in reason


def test_cross_field_rules_are_explained_in_plain_words() -> None:
    reason = _describe(
        {"series_keys": ["SPY"], "start_date": "2026-10-05", "end_date": "2026-10-01"}
    )

    assert "start_date must be on or before end_date" in reason


def test_span_over_the_limit_states_the_limit() -> None:
    reason = _describe(
        {"series_keys": ["SPY"], "start_date": "2010-01-01", "end_date": "2026-01-01"}
    )

    assert "limited to 1830 days" in reason


def test_unknown_fields_are_rejected_and_named() -> None:
    reason = _describe(
        {"series_keys": ["SPY"], "start_date": "2026-10-01", "end_date": "2026-10-02", "sql": "x"}
    )

    assert "sql" in reason


def test_a_long_list_of_problems_is_bounded() -> None:
    reason = _describe({f"bad{n}": n for n in range(50)})

    assert len(reason) <= 400
    assert "more" in reason


@pytest.mark.asyncio
async def test_adapter_raises_a_input_error_whose_reason_is_specific(
    tool_adapter: object,
) -> None:
    adapter: QuantToolAdapter = tool_adapter._adapter  # type: ignore[attr-defined]

    with pytest.raises(ToolInputError) as raised:
        await adapter.execute(
            "quant_get_series", {"series_keys": [MARKER], "limit": 9999}, "bad", 5.0
        )

    assert "limit" in raised.value.reason
    assert MARKER not in raised.value.caller_text
    assert raised.value.retryable is False
