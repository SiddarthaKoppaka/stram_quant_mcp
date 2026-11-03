"""The public tool contract the service must preserve exactly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stram_quant_mcp.errors import ToolInputError
from stram_quant_mcp.tool_registry import TOOL_SPECS
from tests.parity.harness import ALL_SCOPES, ParityClient

CONTRACT = Path(__file__).parents[2] / "contracts/tool_schemas.json"
CONTRACT_FIELDS = ("inputSchema", "outputSchema", "annotations")
ENVELOPE_FIELDS = {
    "tool_version",
    "stram_data_version",
    "interface_version",
    "policy_version",
    "request_id",
    "data",
    "warnings",
}
LEAK_MARKERS = ("pydantic", "validation error", "traceback", "site-packages", "/home")


def _listed_tools(parity: ParityClient) -> dict[str, dict[str, Any]]:
    token = parity.minter.mint(" ".join(ALL_SCOPES))
    tools = parity.rpc("tools/list", token=token).result["tools"]
    return {tool["name"]: tool for tool in tools}


def _contract() -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in json.loads(CONTRACT.read_text())["tools"]}


def test_exactly_the_reviewed_tools_are_listed(parity: ParityClient) -> None:
    assert set(_listed_tools(parity)) == {spec.name for spec in TOOL_SPECS}
    assert len(_listed_tools(parity)) == 10


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_each_tool_matches_the_published_contract(parity: ParityClient, spec: Any) -> None:
    listed = _listed_tools(parity)[spec.name]
    published = _contract()[spec.name]

    for field in CONTRACT_FIELDS:
        assert listed[field] == published[field], field
    assert listed["_meta"]["io.stram/schemaHash"] == spec.schema_hash()
    assert listed["_meta"]["io.stram/requiredScope"] == spec.scope


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_every_tool_is_read_only_and_rejects_unknown_properties(
    parity: ParityClient, spec: Any
) -> None:
    listed = _listed_tools(parity)[spec.name]

    assert listed["annotations"]["readOnlyHint"] is True
    assert listed["annotations"]["destructiveHint"] is False
    assert listed["inputSchema"]["additionalProperties"] is False


def test_a_successful_call_returns_the_documented_envelope(parity: ParityClient) -> None:
    token = parity.minter.mint("quant:discover")

    result = parity.call_tool("quant_list_series", {"limit": 1}, token).result

    assert result["isError"] is False
    assert set(result["structuredContent"]) >= ENVELOPE_FIELDS
    assert len(result["structuredContent"]["data"]["entries"]) == 1


@pytest.mark.parametrize(
    "arguments",
    [{"limit": "not-a-number"}, {"limit": 1, "surplus": True}, {"limit": -5}],
    ids=["wrong-type", "unknown-property", "out-of-bounds"],
)
def test_invalid_arguments_return_one_safe_error(
    parity: ParityClient, arguments: dict[str, object]
) -> None:
    token = parity.minter.mint("quant:discover")

    result = parity.call_tool("quant_list_series", arguments, token).result
    text = result["content"][0]["text"]

    assert result["isError"] is True
    assert text == ToolInputError.public_message
    assert not any(marker in text.lower() for marker in LEAK_MARKERS)


def test_an_unknown_tool_returns_the_same_safe_error(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    result = parity.call_tool("quant_delete_everything", {}, token).result

    assert result["isError"] is True
    assert result["content"][0]["text"] == ToolInputError.public_message
