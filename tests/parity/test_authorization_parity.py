"""Per-tool authorization that every implementation must enforce on every call."""

from __future__ import annotations

from typing import Any

import pytest

from stram_quant_mcp.errors import ToolForbiddenError
from stram_quant_mcp.tool_registry import TOOL_SPECS
from tests.parity.harness import ALL_SCOPES, ParityClient


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_a_token_missing_the_tools_scope_is_denied(parity: ParityClient, spec: Any) -> None:
    other_scopes = " ".join(scope for scope in ALL_SCOPES if scope != spec.scope)
    token = parity.minter.mint(other_scopes)

    result = parity.call_tool(spec.name, {}, token).result

    assert result["isError"] is True
    assert result["content"][0]["text"] == ToolForbiddenError.public_message
    assert "structuredContent" not in result


def test_a_token_with_no_scopes_is_rejected_at_the_transport(parity: ParityClient) -> None:
    token = parity.minter.mint("")

    assert parity.call_tool("quant_list_series", {}, token).status == 401


def test_a_denied_call_is_not_retryable_by_classification() -> None:
    assert ToolForbiddenError.retryable is False
