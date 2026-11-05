"""Stable failures safe to cross the MCP transport boundary.

Every failure carries four fixed, caller-safe facts so a client never has to guess:

* ``code``: the stable machine name (unchanged since V1).
* ``category``: who or what failed (the caller, authorization, our credentials, the Data Plane,
  a deadline, our capacity, or this service).
* ``reason``: one specific sentence on why it failed.
* ``next_action``: what the caller or operator should do, and whether retrying can ever help.

Reasons are chosen from reviewed text or from field names and limits we own. They never contain
upstream response bodies, URLs, tokens, paths, SQL or stack traces.
"""

from __future__ import annotations

from enum import StrEnum

MAX_ID_CHARS = 64
MAX_RETRY_AFTER_SECONDS = 300


class ErrorCategory(StrEnum):
    """Why a call failed, grouped by who can fix it."""

    CALLER_ERROR = "caller_error"
    ACCESS_DENIED = "access_denied"
    SERVICE_CREDENTIALS = "service_credentials"
    DATA_PLANE_UNAVAILABLE = "data_plane_unavailable"
    DATA_PLANE_ERROR = "data_plane_error"
    DEADLINE = "deadline"
    CAPACITY = "capacity"
    RESOURCE_LIMIT = "resource_limit"
    INTERNAL = "internal"


class QuantToolError(Exception):
    """Base error carrying only a stable public classification."""

    code = "internal_error"
    category = ErrorCategory.INTERNAL
    retryable = False
    public_message = "The tool call could not be completed."
    default_reason = "An unexpected internal failure occurred."
    default_next_action = "Do not retry. Report the request_id to the service operator."
    retry_after_seconds: int | None = None

    def __init__(
        self,
        reason: str | None = None,
        *,
        next_action: str | None = None,
        retry_after_seconds: int | None = None,
        upstream_request_id: str | None = None,
    ) -> None:
        """Create the instance."""
        super().__init__(self.code)
        self.reason = reason or self.default_reason
        self.next_action = next_action or self.default_next_action
        if retry_after_seconds is not None:
            self.retry_after_seconds = max(
                1, min(int(retry_after_seconds), MAX_RETRY_AFTER_SECONDS)
            )
        self.upstream_request_id = _safe_identifier(upstream_request_id)

    @property
    def caller_text(self) -> str:
        """The complete, safe, human-readable failure text shown to the caller."""
        retry = "Retryable." if self.retryable else "Not retryable."
        wait = (
            f" Wait at least {self.retry_after_seconds}s first."
            if self.retry_after_seconds is not None
            else ""
        )
        correlation = (
            f" Data Plane request_id: {self.upstream_request_id}."
            if self.upstream_request_id
            else ""
        )
        return (
            f"{self.public_message} Why: {self.reason} "
            f"{retry}{wait} Next: {self.next_action}{correlation}"
        )

    def public_details(self) -> dict[str, object]:
        """Structured twin of ``caller_text`` for clients that branch on fields."""
        details: dict[str, object] = {
            "code": self.code,
            "category": self.category.value,
            "retryable": self.retryable,
            "reason": self.reason,
            "next_action": self.next_action,
        }
        if self.retry_after_seconds is not None:
            details["retry_after_seconds"] = self.retry_after_seconds
        if self.upstream_request_id:
            details["upstream_request_id"] = self.upstream_request_id
        return details


class ToolInputError(QuantToolError):
    """The caller supplied an invalid or out-of-policy request."""

    code = "invalid_request"
    category = ErrorCategory.CALLER_ERROR
    public_message = "The tool arguments are invalid or outside the allowed bounds."
    default_reason = "One or more arguments violate the tool's input schema."
    default_next_action = "Correct the arguments to match the tool schema and call again."


class ToolNotFoundError(QuantToolError):
    """The requested public Data Interface entity was not found."""

    code = "not_found"
    category = ErrorCategory.CALLER_ERROR
    public_message = "The requested quantitative data entity was not found."
    default_reason = "No entity with the given identifier exists in the Data Plane."
    default_next_action = "Check the identifier with a discovery tool; retrying cannot help."


class ToolForbiddenError(QuantToolError):
    """The caller has no grant for this tool; never retryable."""

    code = "access_denied"
    category = ErrorCategory.ACCESS_DENIED
    public_message = "Access denied."
    default_reason = "The caller's token does not grant this tool."
    default_next_action = "Request the required scope from the identity administrator."


class ToolCredentialsError(QuantToolError):
    """This service's own Data Plane credentials were rejected; an operator must fix them."""

    code = "service_credentials_rejected"
    category = ErrorCategory.SERVICE_CREDENTIALS
    public_message = "The service could not authenticate to the Data Plane."
    default_reason = "The Data Plane rejected this service's own credentials."
    default_next_action = (
        "Do not retry. This is not your token: notify the service operator to check the "
        "Data Plane client credentials, scopes and audience."
    )


class ToolUpstreamForbiddenError(QuantToolError):
    """The Data Plane refused this service's token for the route's scope."""

    code = "data_plane_forbidden"
    category = ErrorCategory.SERVICE_CREDENTIALS
    public_message = "The Data Plane refused this service access to the data."
    default_reason = "This service's Data Plane client lacks the scope the route requires."
    default_next_action = (
        "Do not retry. Notify the service operator to grant the missing Data Plane scope."
    )


class ToolUnavailableError(QuantToolError):
    """The Data Plane is temporarily unreachable or unready."""

    code = "unavailable"
    category = ErrorCategory.DATA_PLANE_UNAVAILABLE
    retryable = True
    public_message = "Quantitative data is temporarily unavailable."
    default_reason = "The Data Plane service could not be reached or reported it is not ready."
    default_next_action = "Retry with exponential backoff; if it persists, contact the operator."


class ToolUpstreamError(QuantToolError):
    """The Data Plane answered with an internal error."""

    code = "data_plane_error"
    category = ErrorCategory.DATA_PLANE_ERROR
    public_message = "The Data Plane failed while serving the request."
    default_reason = "The Data Plane returned an internal error for this request."
    default_next_action = (
        "Do not retry immediately. Report the request_id and the Data Plane request_id."
    )


class ToolTimeoutError(QuantToolError):
    """The query exceeded its server-side deadline."""

    code = "deadline_exceeded"
    category = ErrorCategory.DEADLINE
    retryable = True
    public_message = "The quantitative query exceeded its deadline."
    default_reason = "The Data Plane did not answer within this tool's time limit."
    default_next_action = (
        "Retry once with a narrower date range, fewer series or a smaller limit; "
        "do not retry the identical request in a loop."
    )


class ToolOverloadedError(QuantToolError):
    """Admission control or the circuit breaker is shedding load."""

    code = "overloaded"
    category = ErrorCategory.CAPACITY
    retryable = True
    public_message = "The quantitative service is at capacity."
    default_reason = "Too many requests are in flight or the caller exceeded its rate limit."
    default_next_action = "Wait for the indicated delay, then retry with backoff."
    retry_after_seconds = 5


class ToolResourceLimitError(QuantToolError):
    """A response or operation exceeded a deterministic resource budget."""

    code = "resource_limit"
    category = ErrorCategory.RESOURCE_LIMIT
    public_message = "The quantitative result exceeds the allowed resource budget."
    default_reason = "The Data Plane response is larger than this service allows."
    default_next_action = "Do not retry unchanged. Reduce the limit, window or number of series."


def _safe_identifier(value: str | None) -> str | None:
    """Keep only a short opaque correlation id, never free text."""
    if value is None:
        return None
    cleaned = value.strip()
    allowed = cleaned.replace("-", "").replace("_", "")
    if 0 < len(cleaned) <= MAX_ID_CHARS and allowed.isalnum():
        return cleaned
    return None
