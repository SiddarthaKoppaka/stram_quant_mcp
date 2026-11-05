"""What an MCP client sees for each failure category: text, retryability and a next step."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.parity.harness import ALL_SCOPES, ParityClient
from tests.support.fake_data_plane import FakeDataPlane, problem

META = "io.stram/error"


def _call(parity: ParityClient, tool: str, arguments: dict[str, object]) -> dict[str, Any]:
    token = parity.minter.mint(" ".join(ALL_SCOPES))
    return parity.call_tool(tool, arguments, token).result


def _script(plane: FakeDataPlane, response: httpx.Response) -> None:
    plane.script(lambda r: response if r.url.path == "/v1/catalog" else None)


@pytest.mark.parametrize(
    ("response", "category", "retryable"),
    [
        (
            problem(503, "data_unavailable", "x", **{"Retry-After": "9"}),
            "data_plane_unavailable",
            True,
        ),
        (problem(500, "internal_error", "x"), "data_plane_error", False),
        (problem(403, "forbidden", "missing scope: catalog:read"), "service_credentials", False),
        (problem(422, "invalid_query", "limit too large"), "caller_error", False),
        (problem(404, "not_found", "no such thing"), "caller_error", False),
    ],
)
def test_each_failure_cause_reaches_the_client_with_its_own_category(
    parity: ParityClient,
    data_plane: FakeDataPlane,
    response: httpx.Response,
    category: str,
    retryable: bool,
) -> None:
    _script(data_plane, response)

    result = _call(parity, "quant_list_series", {"limit": 1})

    assert result["isError"] is True
    meta = result["_meta"][META]
    assert meta["category"] == category
    assert meta["retryable"] is retryable
    assert meta["reason"] and meta["next_action"]
    text = result["content"][0]["text"]
    assert "Why:" in text and "Next:" in text
    assert ("Retryable." in text) is retryable


def test_a_retry_after_from_the_data_plane_is_passed_to_the_client(
    parity: ParityClient, data_plane: FakeDataPlane
) -> None:
    _script(data_plane, problem(503, "data_unavailable", "x", **{"Retry-After": "9"}))

    meta = _call(parity, "quant_list_series", {"limit": 1})["_meta"][META]

    assert meta["retry_after_seconds"] == 9


def test_invalid_arguments_name_the_field_without_echoing_the_value(parity: ParityClient) -> None:
    result = _call(parity, "quant_list_series", {"limit": "seeded-value"})

    meta = result["_meta"][META]
    assert meta["category"] == "caller_error"
    assert "limit" in meta["reason"]
    assert "seeded-value" not in result["content"][0]["text"]


def test_no_sensitive_upstream_detail_reaches_the_client(
    parity: ParityClient, data_plane: FakeDataPlane
) -> None:
    _script(data_plane, problem(500, "internal_error", "boom at /lake/secret/part-0.parquet"))

    text = _call(parity, "quant_list_series", {"limit": 1})["content"][0]["text"]

    assert "/lake" not in text
    assert "parquet" not in text.lower()
    assert "Traceback" not in text


def test_upstream_request_id_is_offered_for_support_correlation(
    parity: ParityClient, data_plane: FakeDataPlane
) -> None:
    _script(data_plane, problem(500, "internal_error", "x"))

    meta = _call(parity, "quant_list_series", {"limit": 1})["_meta"][META]

    assert meta["upstream_request_id"] == "req-test-1"
