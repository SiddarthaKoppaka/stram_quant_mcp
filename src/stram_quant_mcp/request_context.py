"""Per-request identifiers shared by telemetry, access control and tool dispatch."""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identifiers and start time of the tool call being served."""

    request_id: str
    trace_id: str | None
    started: float


_CURRENT: ContextVar[RequestContext | None] = ContextVar("quant_request_context", default=None)


def begin_request(trace_id: str | None) -> tuple[RequestContext, Token[RequestContext | None]]:
    """Start a request and make it current; pass the token to ``end_request``."""
    context = RequestContext(str(uuid4()), trace_id, time.monotonic())
    return context, _CURRENT.set(context)


def end_request(token: Token[RequestContext | None]) -> None:
    """Stop treating the request as current."""
    _CURRENT.reset(token)


def current_request() -> RequestContext:
    """Return the current request, starting an anonymous one if none was begun."""
    context = _CURRENT.get()
    if context is None:
        context = RequestContext(str(uuid4()), None, time.monotonic())
        _CURRENT.set(context)
    return context
