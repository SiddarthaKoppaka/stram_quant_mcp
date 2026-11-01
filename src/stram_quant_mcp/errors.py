"""Stable failures safe to cross the MCP transport boundary."""

from __future__ import annotations


class QuantToolError(Exception):
    """Base error carrying only a stable public classification."""

    code = "internal_error"
    retryable = False
    public_message = "The tool call could not be completed."


class ToolInputError(QuantToolError):
    """The caller supplied an invalid or out-of-policy request."""

    code = "invalid_request"
    public_message = "The tool arguments are invalid or outside the allowed bounds."


class ToolNotFoundError(QuantToolError):
    """The requested public Data Interface entity was not found."""

    code = "not_found"
    public_message = "The requested quantitative data entity was not found."


class ToolForbiddenError(QuantToolError):
    """The authenticated principal lacks this tool's required scope."""

    code = "forbidden"
    public_message = "The caller is not authorized to invoke this tool."


class ToolUnavailableError(QuantToolError):
    """The Data Plane or isolated query worker is temporarily unavailable."""

    code = "unavailable"
    retryable = True
    public_message = "Quantitative data is temporarily unavailable."


class ToolTimeoutError(QuantToolError):
    """The isolated query exceeded its server-side deadline."""

    code = "deadline_exceeded"
    retryable = True
    public_message = "The quantitative query exceeded its deadline."


class ToolOverloadedError(QuantToolError):
    """The bounded query worker cannot accept more work."""

    code = "overloaded"
    retryable = True
    public_message = "The quantitative service is at capacity."


class ToolResourceLimitError(QuantToolError):
    """A response or operation exceeded a deterministic resource budget."""

    code = "resource_limit"
    public_message = "The quantitative result exceeds the allowed resource budget."
