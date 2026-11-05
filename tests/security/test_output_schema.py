from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from stram_quant_mcp.tool_models import DerivedEnvelope, SeriesEnvelope
from stram_quant_mcp.tool_registry import TOOL_SPECS, exported_definitions
from tests.support.sync_adapter import SyncAdapter

ROOT = Path(__file__).parents[2]


def test_checked_in_tool_definitions_are_current_and_closed() -> None:
    checked_in = json.loads((ROOT / "contracts/tool_schemas.json").read_text())
    assert checked_in == exported_definitions()
    for tool in checked_in["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"] == {
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
            "readOnlyHint": True,
        }
        assert len(tool["schemaHash"]) == 64


def test_every_real_result_round_trips_through_its_output_contract(
    tool_adapter: SyncAdapter,
) -> None:
    examples: dict[str, dict[str, object]] = {
        "quant_list_series": {"limit": 2},
        "quant_list_indicators": {},
        "quant_list_indicator_subjects": {"indicator_id": "equity_return_1d"},
        "quant_describe_availability": {},
        "quant_get_freshness": {},
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
            "availability": "ex_post",
        },
        "quant_get_conditional_returns": {
            "target_symbol": "SPY",
            "conditions": [{"series_key": "UST_10YEAR", "operator": "gt", "threshold": 0.0}],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "horizons": [1],
            "availability": "ex_post",
        },
    }
    derived = tool_adapter.execute("quant_get_derived", examples["quant_get_derived"], "seed")
    assert isinstance(derived, DerivedEnvelope)
    examples["quant_get_lineage"] = {"event_id": derived.data.rows[0].event_id}

    for spec in TOOL_SPECS:
        result = tool_adapter.execute(spec.name, examples[spec.name], spec.name)
        serialized = result.model_dump(mode="json")
        validated: BaseModel = spec.output_model.model_validate(serialized)
        assert validated.model_dump(mode="json") == serialized


def test_output_contract_preserves_missing_values_flags_and_retractions() -> None:
    common = {
        "tool_version": "0.1.0",
        "stram_data_version": "1.3.1",
        "interface_version": "1",
        "policy_version": "test",
        "request_id": "edge-cases",
    }
    missing = SeriesEnvelope.model_validate(
        {
            **common,
            "data": {
                "rows": [
                    {
                        "dataset": "economic_observation",
                        "kind": "economic",
                        "provider": "fred",
                        "series_key": "TEST",
                        "indicator_id": None,
                        "observed_at": "2026-10-01T00:00:00Z",
                        "observed_on": "2026-10-01",
                        "value": None,
                        "unit": "Index",
                        "known_since": "2026-10-02",
                        "is_vintage": True,
                        "ingested_at": "2026-10-02T01:00:00Z",
                        "event_id": "event",
                        "record_id": "record",
                        "quality_flags": ["missing_value"],
                    }
                ],
                "count": 1,
                "limit": 100,
                "truncated": False,
                "as_of": None,
                "all_versions": False,
                "availability_note": "test",
            },
        }
    ).model_dump(mode="json")
    retracted = DerivedEnvelope.model_validate(
        {
            **common,
            "data": {
                "rows": [
                    {
                        "event_id": "derived-event",
                        "indicator_id": "test_indicator",
                        "indicator_version": "1",
                        "subject": None,
                        "observation_date": "2026-10-01",
                        "value": None,
                        "unit": "Index",
                        "known_since": "2026-10-02",
                        "computed_at": "2026-10-02T01:00:00Z",
                        "input_event_ids": [],
                        "input_batch_ids": [],
                        "availability_basis": None,
                        "formula": None,
                        "licensed_input": False,
                        "retracted": True,
                    }
                ],
                "count": 1,
                "limit": 100,
                "truncated": False,
                "as_of": None,
                "availability_note": "test",
            },
        }
    ).model_dump(mode="json")

    assert missing["data"]["rows"][0]["value"] is None
    assert missing["data"]["rows"][0]["quality_flags"] == ["missing_value"]
    assert retracted["data"]["rows"][0]["value"] is None
    assert retracted["data"]["rows"][0]["retracted"] is True
