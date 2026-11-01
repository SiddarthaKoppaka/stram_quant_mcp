from __future__ import annotations

from decimal import Decimal

import pytest

from stram_quant_mcp.errors import ToolInputError, ToolNotFoundError
from stram_quant_mcp.tool_adapter import QuantToolAdapter
from stram_quant_mcp.tool_models import (
    AvailabilityPoliciesEnvelope,
    CatalogEnvelope,
    DerivedEnvelope,
    FreshnessEnvelope,
    IndicatorsEnvelope,
    IndicatorSubjectsEnvelope,
    LineageEnvelope,
    SeriesEnvelope,
)


def test_all_eight_read_and_discovery_tools_use_the_real_interface(
    tool_adapter: QuantToolAdapter,
) -> None:
    catalog = tool_adapter.execute("quant_list_series", {"limit": 20}, "catalog")
    assert isinstance(catalog, CatalogEnvelope)
    assert {entry.dataset for entry in catalog.data.entries} == {
        "economic_observation",
        "market_bar",
        "positioning_report",
    }

    indicators = tool_adapter.execute("quant_list_indicators", {}, "indicators")
    assert isinstance(indicators, IndicatorsEnvelope)
    assert {item.indicator_id for item in indicators.data.indicators} >= {
        "equity_return_1d",
        "treasury_10y_minus_2y",
    }

    subjects = tool_adapter.execute(
        "quant_list_indicator_subjects",
        {"indicator_id": "equity_return_1d"},
        "subjects",
    )
    assert isinstance(subjects, IndicatorSubjectsEnvelope)
    assert subjects.data.subjects == ("SPY",)

    policies = tool_adapter.execute("quant_describe_availability", {}, "policies")
    assert isinstance(policies, AvailabilityPoliciesEnvelope)
    assert {item.policy_id for item in policies.data.policies} >= {
        "source_vintage",
        "ex_post",
    }

    freshness = tool_adapter.execute("quant_get_freshness", {}, "freshness")
    assert isinstance(freshness, FreshnessEnvelope)
    assert {item.dataset for item in freshness.data.datasets} >= {
        "economic_observation",
        "market_bar",
        "derived_indicator",
    }

    series = tool_adapter.execute(
        "quant_get_series",
        {
            "series_keys": ["CPIAUCSL"],
            "kinds": ["economic"],
            "start_date": "2026-06-01",
            "end_date": "2026-07-31",
            "all_versions": True,
        },
        "series",
    )
    assert isinstance(series, SeriesEnvelope)
    assert [row.value for row in series.data.rows] == [100.0, 101.0, 102.0]
    assert [row.known_since.isoformat() for row in series.data.rows] == [
        "2026-07-15",
        "2026-08-14",
        "2026-09-15",
    ]

    derived = tool_adapter.execute(
        "quant_get_derived",
        {
            "indicator_id": "treasury_10y_minus_2y",
            "start_date": "2026-10-01",
            "end_date": "2026-10-01",
        },
        "derived",
    )
    assert isinstance(derived, DerivedEnvelope)
    assert derived.data.rows[0].value == Decimal("0.400000000000")
    assert derived.model_dump(mode="json")["data"]["rows"][0]["value"] == "0.400000000000"

    lineage = tool_adapter.execute(
        "quant_get_lineage",
        {"event_id": derived.data.rows[0].event_id},
        "lineage",
    )
    assert isinstance(lineage, LineageEnvelope)
    assert {item.series_key for item in lineage.data.inputs} == {"UST_10YEAR", "UST_2YEAR"}
    assert lineage.data.unresolved_input_ids == ()


def test_truncation_and_hindsight_are_explicit(tool_adapter: QuantToolAdapter) -> None:
    truncated = tool_adapter.execute(
        "quant_get_series",
        {
            "series_keys": ["SPY"],
            "kinds": ["market_bar"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "limit": 1,
        },
        "truncated",
    )
    assert isinstance(truncated, SeriesEnvelope)
    assert truncated.data.truncated is True
    assert any("truncated" in warning.lower() for warning in truncated.warnings)

    hindsight = tool_adapter.execute(
        "quant_get_series",
        {
            "series_keys": ["SPY"],
            "kinds": ["market_bar"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "availability": "ex_post",
        },
        "hindsight",
    )
    assert isinstance(hindsight, SeriesEnvelope)
    assert hindsight.data.availability.hindsight_only is True
    assert any("hindsight-only" in warning for warning in hindsight.warnings)


def test_strict_prevents_lookahead_and_estimated_preserves_evidence(
    tool_adapter: QuantToolAdapter,
) -> None:
    common = {
        "series_keys": ["UST_10YEAR"],
        "kinds": ["rates"],
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
        "as_of": "2026-10-02",
    }
    strict = tool_adapter.execute("quant_get_series", common, "strict")
    estimated = tool_adapter.execute(
        "quant_get_series", {**common, "availability": "estimated"}, "estimated"
    )

    assert isinstance(strict, SeriesEnvelope) and strict.data.count == 0
    assert isinstance(estimated, SeriesEnvelope) and estimated.data.count == 1
    evidence = estimated.data.rows[0].availability
    assert evidence is not None
    assert evidence.evidence == "modeled_delay"
    assert evidence.verified is False
    assert estimated.data.availability.hindsight_only is False


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {
            "series_keys": ["SPY"],
            "start_date": "2026-10-02",
            "end_date": "2026-10-01",
        },
        {
            "series_keys": ["SPY"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "limit": 501,
        },
        {
            "series_keys": ["SPY; SELECT 1"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
        },
        {
            "series_keys": ["SPY"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "availability": "ex_post",
            "as_of": "2026-10-02",
        },
        {
            "series_keys": ["SPY"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "sql": "select 1",
        },
    ],
)
def test_invalid_series_requests_fail_with_one_safe_error(
    tool_adapter: QuantToolAdapter, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolInputError) as error:
        tool_adapter.execute("quant_get_series", arguments, "bad")
    assert error.value.code == "invalid_request"
    assert "select" not in error.value.public_message.lower()


def test_unknown_entities_and_tools_have_stable_errors(tool_adapter: QuantToolAdapter) -> None:
    with pytest.raises(ToolNotFoundError):
        tool_adapter.execute(
            "quant_list_indicator_subjects", {"indicator_id": "not_real"}, "missing"
        )
    with pytest.raises(ToolInputError):
        tool_adapter.execute("not_a_tool", {}, "unknown")
