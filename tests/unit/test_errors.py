"""Every failure must say what failed, why, whether retrying helps, and what to do next."""

from __future__ import annotations

import pytest

from stram_quant_mcp import errors
from stram_quant_mcp.errors import ErrorCategory, QuantToolError

ALL_ERRORS = [
    errors.ToolInputError,
    errors.ToolNotFoundError,
    errors.ToolForbiddenError,
    errors.ToolCredentialsError,
    errors.ToolUpstreamForbiddenError,
    errors.ToolUnavailableError,
    errors.ToolUpstreamError,
    errors.ToolTimeoutError,
    errors.ToolOverloadedError,
    errors.ToolResourceLimitError,
]
RETRYABLE = {
    errors.ToolUnavailableError,
    errors.ToolTimeoutError,
    errors.ToolOverloadedError,
}


@pytest.mark.parametrize("error_type", ALL_ERRORS)
def test_every_error_states_why_and_what_to_do_next(error_type: type[QuantToolError]) -> None:
    text = error_type().caller_text

    assert "Why:" in text
    assert "Next:" in text
    assert ("Retryable." in text) == (error_type in RETRYABLE)
    assert ("Not retryable." in text) == (error_type not in RETRYABLE)


@pytest.mark.parametrize("error_type", ALL_ERRORS)
def test_codes_are_unique_and_categories_are_declared(error_type: type[QuantToolError]) -> None:
    assert isinstance(error_type.category, ErrorCategory)
    assert [item.code for item in ALL_ERRORS].count(error_type.code) == 1


def test_retry_categories_distinguish_access_credentials_and_outages() -> None:
    assert errors.ToolForbiddenError.retryable is False
    assert errors.ToolForbiddenError.category is ErrorCategory.ACCESS_DENIED
    assert errors.ToolCredentialsError.category is ErrorCategory.SERVICE_CREDENTIALS
    assert errors.ToolCredentialsError.retryable is False
    assert errors.ToolUnavailableError.category is ErrorCategory.DATA_PLANE_UNAVAILABLE
    assert errors.ToolUnavailableError.retryable is True
    assert errors.ToolUpstreamError.category is ErrorCategory.DATA_PLANE_ERROR
    assert errors.ToolUpstreamError.retryable is False


def test_a_specific_reason_and_wait_replace_the_defaults() -> None:
    error = errors.ToolUnavailableError("The Data Plane is down.", retry_after_seconds=7)

    details = error.public_details()

    assert details["reason"] == "The Data Plane is down."
    assert details["retry_after_seconds"] == 7
    assert "Wait at least 7s first." in error.caller_text


def test_retry_after_is_bounded() -> None:
    assert errors.ToolUnavailableError(retry_after_seconds=10_000).retry_after_seconds == 300
    assert errors.ToolUnavailableError(retry_after_seconds=0).retry_after_seconds == 1


@pytest.mark.parametrize("hostile", ["req 1; DROP TABLE", "/etc/passwd", "x" * 200, "a b", ""])
def test_only_opaque_upstream_request_ids_are_echoed(hostile: str) -> None:
    assert errors.ToolUpstreamError(upstream_request_id=hostile).upstream_request_id is None


def test_a_clean_upstream_request_id_is_echoed() -> None:
    error = errors.ToolUpstreamError(upstream_request_id="req-42_a")

    assert error.public_details()["upstream_request_id"] == "req-42_a"
    assert "req-42_a" in error.caller_text
