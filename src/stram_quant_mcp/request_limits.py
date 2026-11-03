"""ASGI guard that bounds the size of a request body before the application reads it."""

from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOO_LARGE_STATUS = 413
TOO_LARGE_TEXT = "Request body too large"


class _BodyTooLargeError(Exception):
    """Raised inside the receive wrapper when the streamed body passes the limit."""


class BodyLimitMiddleware:
    """Rejects a request whose body exceeds ``max_bytes``, declared or streamed."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """Create the guard.

        Args:
            app: The application to protect.
            max_bytes: Largest permitted request body.
        """
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass HTTP requests through a size check; other scopes are untouched."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if self._declared_length(scope) > self._max_bytes:
            await self._reject(scope, receive, send)
            return
        started = False
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self._app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if not started:
                await self._reject(scope, receive, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit():
                return int(value)
        return 0

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse(TOO_LARGE_TEXT, status_code=TOO_LARGE_STATUS)
        await response(scope, receive, send)
