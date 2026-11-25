from __future__ import annotations

from decimal import Decimal

import pytest

from stram_quant_mcp.errors import ToolInputError, ToolNotFoundError
from stram_quant_mcp.tool_models import (
    AvailabilityPoliciesEnvelope,
    CatalogEnvelope,
    DerivedEnvelope,
    FreshnessEnvelope,
    FundamentalRatiosEnvelope,
    FundamentalsEnvelope,
    IndicatorsEnvelope,
    IndicatorSubjectsEnvelope,
    LatestQuotesEnvelope,
    LineageEnvelope,
    SeriesEnvelope,
)
from tests.support.fake_data_plane import EXACT_RATIO
from tests.support.sync_adapter import SyncAdapter


def test_all_read_and_discovery_tools_use_the_real_interface(
    tool_adapter: SyncAdapter,
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

    fundamentals = tool_adapter.execute(
        "quant_get_fundamentals", {"symbols": ["AAPL", "SPY"]}, "fundamentals"
    )
    assert isinstance(fundamentals, FundamentalsEnvelope)
    apple, spy = fundamentals.data.companies
    assert (apple.symbol, apple.pe_ttm, spy.symbol, spy.unavailable) == (
        "AAPL",
        Decimal("39.69"),
        "SPY",
        "not a tracked company with SEC filings",
    )
    assert [metric.metric for metric in apple.metrics] == ["revenue", "eps_diluted"]

    latest = tool_adapter.execute("quant_get_latest_quotes", {"symbols": ["SPY", "AAPL"]}, "quotes")
    assert isinstance(latest, LatestQuotesEnvelope)
    assert [quote.symbol for quote in latest.data.quotes] == ["SPY", "AAPL"]
    assert all(quote.status == "ok" and quote.quote is not None for quote in latest.data.quotes)


def test_latest_quotes_request_the_licensed_data_scope_and_real_route(
    tool_adapter: SyncAdapter, data_plane
) -> None:  # type: ignore[no-untyped-def]
    latest = tool_adapter.execute("quant_get_latest_quotes", {"symbols": ["SPY"]}, "quotes")

    assert isinstance(latest, LatestQuotesEnvelope)
    assert [form.get("scope") for form in data_plane.token_requests] == ["market%3Aiex"]
    assert len(data_plane.calls_to("/v1/market/quotes")) == 1


def test_truncation_and_hindsight_are_explicit(tool_adapter: SyncAdapter) -> None:
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
    tool_adapter: SyncAdapter,
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
            "limit": 5001,
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
    tool_adapter: SyncAdapter, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolInputError) as error:
        tool_adapter.execute("quant_get_series", arguments, "bad")
    assert error.value.code == "invalid_request"
    assert "select" not in error.value.public_message.lower()


def test_unknown_entities_and_tools_have_stable_errors(tool_adapter: SyncAdapter) -> None:
    with pytest.raises(ToolNotFoundError):
        tool_adapter.execute(
            "quant_list_indicator_subjects", {"indicator_id": "not_real"}, "missing"
        )
    with pytest.raises(ToolInputError):
        tool_adapter.execute("not_a_tool", {}, "unknown")


def test_ratios_keep_their_exact_decimals_formulas_and_reasons(
    tool_adapter: SyncAdapter, data_plane
) -> None:  # type: ignore[no-untyped-def]
    result = tool_adapter.execute(
        "quant_get_fundamental_ratios", {"symbols": ["AAPL", "SPY"]}, "ratios"
    )

    assert isinstance(result, FundamentalRatiosEnvelope)
    apple, spy = result.data.companies
    ratios = {ratio.name: ratio for ratio in apple.ratios}
    assert ratios["gross_margin"].value == EXACT_RATIO  # every digit, never a float
    assert ratios["gross_margin"].formula == "gross_profit / revenue"
    assert ratios["gross_margin"].inputs[0].caveat is not None
    assert ratios["current_ratio"].value is None  # missing stays distinct from zero
    assert ratios["current_ratio"].note == "missing: current_assets, current_liabilities"
    assert spy.unavailable == "not a tracked company with SEC filings" and spy.ratios == ()
    assert len(data_plane.calls_to("/v1/fundamentals/ratios")) == 1


def test_ratio_groups_narrow_the_request(tool_adapter: SyncAdapter) -> None:
    result = tool_adapter.execute(
        "quant_get_fundamental_ratios", {"symbols": ["AAPL"], "groups": ["liquidity"]}, "groups"
    )

    assert isinstance(result, FundamentalRatiosEnvelope)
    assert [ratio.group for ratio in result.data.companies[0].ratios] == ["liquidity"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"symbols": ["AAPL", "AAPL"]},
        {"symbols": ["AAPL"], "groups": ["growth", "growth"]},
        {"symbols": ["AAPL"], "groups": ["vibes"]},
        {"symbols": ["aapl"]},
        {"symbols": []},
        {"symbols": [f"S{i}" for i in range(11)]},
        {"symbols": ["AAPL"], "as_of": "not a date"},
        {"symbols": ["AAPL"], "sql": "select 1"},
    ],
)
def test_ratio_inputs_are_closed_and_bounded(
    tool_adapter: SyncAdapter, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolInputError):
        tool_adapter.execute("quant_get_fundamental_ratios", arguments, "bad")
