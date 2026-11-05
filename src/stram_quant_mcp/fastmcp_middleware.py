"""Cross-cutting behavior for the FastMCP server: telemetry, safe errors and access control.

Middleware runs outermost first, in this order: telemetry, error shaping, access control.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import mcp_types as mt
from fastmcp.exceptions import NotFoundError
from fastmcp.exceptions import ValidationError as FastMcpValidationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from stram_quant_mcp.access_policy import (
    AccessPolicyStore,
    CallerIdentity,
    DenialReason,
    identity_from_token,
)
from stram_quant_mcp.denial_throttle import DenialThrottle
from stram_quant_mcp.errors import (
    QuantToolError,
    ToolForbiddenError,
    ToolInputError,
    ToolUnavailableError,
)
from stram_quant_mcp.fastmcp_audit import PreDispatchAuditor
from stram_quant_mcp.request_context import begin_request, current_request, end_request
from stram_quant_mcp.telemetry import Telemetry, correlation_ids
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME

CallContext = MiddlewareContext[mt.CallToolRequestParams]
ListContext = MiddlewareContext[mt.ListToolsRequest]
MAX_CAUSE_DEPTH = 5
ERROR_META_KEY = "io.stram/error"
DENIED = "denied"
ALLOWED = "allowed"
OUTCOME_BY_CODE = {
    "access_denied": "denied",
    "overloaded": "overloaded",
    "deadline_exceeded": "overloaded",
    "invalid_request": "invalid_request",
    "not_found": "invalid_request",
    "unavailable": "unavailable",
    "service_credentials_rejected": "unavailable",
    "data_plane_forbidden": "unavailable",
    "data_plane_error": "unavailable",
    "resource_limit": "invalid_request",
}
DENIAL_AUDIT = {
    DenialReason.UNKNOWN_TOOL: (DENIED, "invalid_tool", ToolInputError.__name__),
    DenialReason.THROTTLED: (DENIED, "denial_throttled", ToolForbiddenError.__name__),
}
DEFAULT_DENIAL_AUDIT = (DENIED, "authorization_denied", ToolForbiddenError.__name__)


def error_result(error: QuantToolError) -> ToolResult:
    """Build the one safe error reply: what failed, why, whether retrying helps, and what next.

    The text is for people and models; the same facts are in ``meta`` as fields to branch on.
    """
    return ToolResult(
        content=[TextContent(type="text", text=error.caller_text)],
        is_error=True,
        meta={ERROR_META_KEY: error.public_details()},
    )


class TelemetryMiddleware(Middleware):
    """Wraps each tool call in a span and emits one completion event and metric."""

    def __init__(self, telemetry: Telemetry) -> None:
        """Create the middleware.

        Args:
            telemetry: Safe-field-only telemetry sink.
        """
        self._telemetry = telemetry

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> ToolResult:
        """Time the call and record only a fixed set of safe fields."""
        name = context.message.name if context.message.name in TOOLS_BY_NAME else "unknown"
        arguments = context.message.arguments or {}
        mode = arguments.get("availability")
        availability_mode = mode if isinstance(mode, str) else "none"
        with self._telemetry.start_span("stram.quant.tool", attributes={"gen_ai.tool.name": name}):
            trace_id, span_id = correlation_ids()
            request, token = begin_request(trace_id)
            try:
                result: ToolResult = await call_next(context)
            finally:
                end_request(token)
            outcome = _telemetry_outcome(result)
            duration = _elapsed_seconds(request.started)
            self._telemetry.tool_result(name, outcome, availability_mode, duration)
            self._telemetry.events.emit(
                "tool.completed",
                trace_id=trace_id,
                span_id=span_id,
                request_id=request.request_id,
                tool=name,
                outcome=outcome,
                duration_ms=duration * 1000,
            )
            return result


class ErrorShapingMiddleware(Middleware):
    """Turns every failure into a fixed, safe, classified reply.

    No exception text, validation detail, path or stack trace can reach the caller.
    """

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> ToolResult:
        """Return the tool result or a safe error result."""
        try:
            result: ToolResult = await call_next(context)
            return result
        except Exception as error:  # noqa: BLE001 - request boundary: cancellation is a BaseException
            return error_result(_classify(error))


class AccessControlMiddleware(Middleware):
    """Re-checks authorization on every call and hides tools the caller cannot use.

    The policy store decides both what is listed and what may run, and a caller that keeps being
    denied is throttled. A denied call never reaches the tool.
    """

    def __init__(
        self,
        store: AccessPolicyStore,
        throttle: DenialThrottle,
        auditor: PreDispatchAuditor,
        tenant_claim: str,
    ) -> None:
        """Create the middleware.

        Args:
            store: The authority on who may call which tool.
            throttle: Blocks callers who keep being denied.
            auditor: Writes the terminal record for refused calls.
            tenant_claim: Name of the verified tenant claim.
        """
        self._store = store
        self._throttle = throttle
        self._auditor = auditor
        self._tenant_claim = tenant_claim

    async def on_list_tools(
        self, context: ListContext, call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]]
    ) -> Sequence[Tool]:
        """List only the tools this caller may call, and none to a throttled caller."""
        tools = await call_next(context)
        identity = self._identity()
        principal = self._auditor.principal_hash(identity)
        if principal is not None and self._throttle.is_blocked(principal):
            return []
        visible = self._store.visible_tools(identity)
        return [tool for tool in tools if tool.name in visible]

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> ToolResult:
        """Run the call only if the policy store grants it; otherwise refuse and audit."""
        name = context.message.name
        identity = self._identity()
        principal = self._auditor.principal_hash(identity)
        reason = self._denial_reason(identity, principal, name)
        if reason is None:
            allowed: ToolResult = await call_next(context)
            return allowed
        if principal is not None:
            self._throttle.record_denial(principal)
        audit = DENIAL_AUDIT.get(reason, DEFAULT_DENIAL_AUDIT)
        self._auditor.record(name, identity, current_request(), audit)
        if reason is DenialReason.UNKNOWN_TOOL:
            raise ToolInputError(f"{name[:60]!r} is not a tool of this service.")
        if reason is DenialReason.THROTTLED:
            raise ToolForbiddenError(
                "This caller made too many denied calls recently and is blocked from all tools.",
                next_action=(
                    "Stop calling tools the token is not scoped for; the block lifts after the "
                    "denial window passes without further denied calls."
                ),
            )
        raise ToolForbiddenError

    def _identity(self) -> CallerIdentity | None:
        return identity_from_token(get_access_token(), self._tenant_claim)

    def _denial_reason(
        self, identity: CallerIdentity | None, principal: str | None, name: str
    ) -> DenialReason | None:
        if principal is not None and self._throttle.is_blocked(principal):
            return DenialReason.THROTTLED
        if name not in TOOLS_BY_NAME:
            return DenialReason.UNKNOWN_TOOL
        return self._store.decide(identity, name).reason


def _classify(error: Exception) -> QuantToolError:
    """Map any failure to a classified error, looking through the framework's wrapping."""
    current: BaseException | None = error
    for _ in range(MAX_CAUSE_DEPTH):
        if isinstance(current, QuantToolError):
            return current
        if isinstance(current, FastMcpValidationError | NotFoundError):
            return ToolInputError()
        if current is None:
            break
        current = current.__cause__
    return ToolUnavailableError()


def _telemetry_outcome(result: Any) -> str:
    if not getattr(result, "is_error", False):
        return "success"
    meta = getattr(result, "meta", None) or {}
    code = (meta.get(ERROR_META_KEY) or {}).get("code")
    return OUTCOME_BY_CODE.get(str(code), "error")


def _elapsed_seconds(started: float) -> float:
    return time.monotonic() - started
