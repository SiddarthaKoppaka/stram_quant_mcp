"""Authenticated HTTP client for the Stram Data Plane service.

This is the only module that talks to the Data Plane. It owns three things and nothing else:

* the service's own ``client_credentials`` token (cached in memory, refreshed before expiry,
  never logged);
* bounded, classified request execution; and
* translation of every failure into a ``QuantToolError`` that says why it failed and whether
  retrying can help.

It performs no query logic. Bodies and responses are the Data Interface's own models.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from stram_quant_mcp.errors import (
    QuantToolError,
    ToolCredentialsError,
    ToolInputError,
    ToolNotFoundError,
    ToolResourceLimitError,
    ToolTimeoutError,
    ToolUnavailableError,
    ToolUpstreamError,
    ToolUpstreamForbiddenError,
)

TOKEN_REFRESH_MARGIN_SECONDS = 30.0
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_UPSTREAM_REASON_CHARS = 240
HINDSIGHT_SCOPE = "hindsight:read"
MARKET_IEX_SCOPE = "market:iex"
TOKEN_REJECTED = frozenset({HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})
_SCOPE_PATTERN = re.compile(r"[a-z_]+:[a-z_<>]+")
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}")
_CONTROL_PATTERN = re.compile(r"[^\x20-\x7e]")


class DataPlaneSettings(BaseModel):
    """Validated, immutable connection settings; the client secret is file-backed."""

    model_config = ConfigDict(extra="forbid", frozen=True, url_preserve_empty_path=True)

    base_url: AnyHttpUrl
    token_url: AnyHttpUrl
    client_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")
    client_secret_path: Path
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    read_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    allow_insecure_http: bool = False


@dataclass(frozen=True, slots=True)
class _Call:
    method: str
    path: str
    body: Mapping[str, object] | None
    params: Mapping[str, str | int] | None
    timeout_seconds: float | None
    extra_scope: str | None


@dataclass(slots=True)
class _CachedToken:
    value: str
    refresh_at: float


class TokenProvider:
    """Single-flight ``client_credentials`` token cache, keyed by the optional extra scope."""

    def __init__(
        self,
        settings: DataPlaneSettings,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Fetch tokens with ``http`` using the credentials in ``settings``."""
        self._settings = settings
        self._http = http
        self._clock = clock
        self._tokens: dict[str | None, _CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get(self, extra_scope: str | None = None) -> str:
        """Return a valid bearer token, refreshing it shortly before it expires."""
        cached = self._tokens.get(extra_scope)
        if cached is not None and self._clock() < cached.refresh_at:
            return cached.value
        async with self._lock:
            cached = self._tokens.get(extra_scope)
            if cached is not None and self._clock() < cached.refresh_at:
                return cached.value
            fresh = await self._fetch(extra_scope)
            self._tokens[extra_scope] = fresh
            return fresh.value

    def invalidate(self, extra_scope: str | None = None) -> None:
        """Drop a token the Data Plane rejected so the next call fetches a new one."""
        self._tokens.pop(extra_scope, None)

    async def _fetch(self, extra_scope: str | None) -> _CachedToken:
        try:
            secret = self._settings.client_secret_path.read_text().strip()
        except OSError:
            raise ToolCredentialsError(
                "The Data Plane client secret file could not be read.",
                next_action="Do not retry. Notify the operator to check the mounted secret file.",
            ) from None
        if not secret:
            raise ToolCredentialsError("The Data Plane client secret file is empty.") from None
        form = {
            "grant_type": "client_credentials",
            "client_id": self._settings.client_id,
            "client_secret": secret,
        }
        if extra_scope:
            form["scope"] = extra_scope
        try:
            response = await self._http.post(
                str(self._settings.token_url),
                data=form,
                timeout=httpx.Timeout(
                    self._settings.read_timeout_seconds,
                    connect=self._settings.connect_timeout_seconds,
                ),
            )
        except httpx.TimeoutException:
            raise ToolTimeoutError(
                "The identity provider did not answer the token request in time."
            ) from None
        except httpx.TransportError:
            raise ToolUnavailableError(
                "The identity provider could not be reached to issue a service token."
            ) from None
        if response.status_code in TOKEN_REJECTED:
            raise ToolCredentialsError(
                "The identity provider rejected the service's client credentials or "
                "requested scope."
            ) from None
        if response.status_code != HTTPStatus.OK:
            raise ToolUnavailableError(
                "The identity provider failed to issue a service token."
            ) from None
        try:
            body = response.json()
            token = body["access_token"]
            lifetime = float(body.get("expires_in", 60))
        except (ValueError, KeyError, TypeError):
            raise ToolUpstreamError(
                "The identity provider returned a malformed token response."
            ) from None
        if not isinstance(token, str) or not token:
            raise ToolUpstreamError("The identity provider returned an empty token.") from None
        refresh_after = max(1.0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS)
        return _CachedToken(token, self._clock() + refresh_after)


class DataPlaneClient:
    """Bounded, classified access to the Data Plane's read-only routes."""

    def __init__(
        self,
        settings: DataPlaneSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Fetch tokens with ``http`` using the credentials in ``settings``."""
        self._settings = settings
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._tokens = TokenProvider(settings, self._http, clock=clock)
        self._base = str(settings.base_url).rstrip("/")

    async def aclose(self) -> None:
        """Close pooled connections."""
        await self._http.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        timeout_seconds: float | None = None,
        extra_scope: str | None = None,
    ) -> bytes:
        """GET a route and return the raw 200 body, or raise a classified error."""
        return await self._send(_Call("GET", path, None, params, timeout_seconds, extra_scope))

    async def post_json(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
        extra_scope: str | None = None,
    ) -> bytes:
        """POST a JSON query and return the raw 200 body, or raise a classified error."""
        return await self._send(_Call("POST", path, body, None, timeout_seconds, extra_scope))

    async def get_public(self, path: str) -> bytes:
        """Fetch an unauthenticated route such as the OpenAPI document."""
        try:
            response = await self._http.get(f"{self._base}{path}", timeout=self._timeout(None))
        except httpx.TimeoutException:
            raise ToolTimeoutError("The Data Plane did not answer in time.") from None
        except httpx.TransportError:
            raise ToolUnavailableError("The Data Plane service could not be reached.") from None
        if response.status_code != HTTPStatus.OK:
            raise _classify_status(response)
        return response.content

    def _timeout(self, timeout_seconds: float | None) -> httpx.Timeout:
        read = timeout_seconds or self._settings.read_timeout_seconds
        return httpx.Timeout(read, connect=self._settings.connect_timeout_seconds)

    async def _send(self, call: _Call) -> bytes:
        extra_scope = call.extra_scope
        refreshed = False
        reconnected = False
        while True:
            token = await self._tokens.get(extra_scope)
            try:
                response = await self._http.request(
                    call.method,
                    f"{self._base}{call.path}",
                    json=call.body,
                    params=call.params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=self._timeout(call.timeout_seconds),
                )
            except httpx.TimeoutException:
                raise ToolTimeoutError(
                    "The Data Plane did not answer within this tool's time limit."
                ) from None
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                # Every route is read-only and idempotent, so one reconnect is always safe.
                if not reconnected:
                    reconnected = True
                    continue
                raise ToolUnavailableError(
                    "The Data Plane connection failed twice (refused, reset or closed)."
                ) from None
            except httpx.TransportError:
                raise ToolUnavailableError("The Data Plane service could not be reached.") from None
            if response.status_code == HTTPStatus.UNAUTHORIZED and not refreshed:
                refreshed = True
                self._tokens.invalidate(extra_scope)
                continue
            if response.status_code == HTTPStatus.OK:
                if len(response.content) > self._settings.max_response_bytes:
                    raise ToolResourceLimitError(
                        "The Data Plane response exceeds this service's response size cap."
                    )
                return response.content
            raise _classify_status(response)


class _Problem:
    """The parts of a Data Plane error reply that are safe to use."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.code, self.message, self.request_id = _problem(response)


def _invalid_credentials(_: _Problem) -> QuantToolError:
    return ToolCredentialsError(
        "The Data Plane rejected the service token, even after a fresh one was issued "
        "(wrong audience, issuer or expired signing key)."
    )


def _forbidden(problem: _Problem) -> QuantToolError:
    scope = _SCOPE_PATTERN.search(problem.message or "")
    named = f" (missing scope {scope.group(0)})" if scope else ""
    return ToolUpstreamForbiddenError(
        f"The Data Plane refused this service's token for the route{named}.",
        upstream_request_id=problem.request_id,
    )


def _missing(problem: _Problem) -> QuantToolError:
    return ToolNotFoundError(
        _sanitize(problem.message) or "The Data Plane has no such entity.",
        upstream_request_id=problem.request_id,
    )


def _invalid_query(problem: _Problem) -> QuantToolError:
    return ToolInputError(
        _sanitize(problem.message) or "The Data Plane rejected the query as invalid.",
        next_action="Correct the argument named in the reason and call again.",
        upstream_request_id=problem.request_id,
    )


def _unavailable(problem: _Problem) -> QuantToolError:
    status = problem.response.status_code
    what = {
        HTTPStatus.SERVICE_UNAVAILABLE: "reported its data is temporarily unavailable",
        HTTPStatus.BAD_GATEWAY: "is behind a failing gateway",
        HTTPStatus.GATEWAY_TIMEOUT: "is behind a gateway that timed out",
        HTTPStatus.TOO_MANY_REQUESTS: "is rate limiting this service",
    }.get(HTTPStatus(status), "is unavailable")
    return ToolUnavailableError(
        f"The Data Plane {what} (HTTP {status}).",
        retry_after_seconds=_retry_after(problem.response),
        upstream_request_id=problem.request_id,
    )


_STATUS_HANDLERS: dict[int, Callable[[_Problem], QuantToolError]] = {
    HTTPStatus.UNAUTHORIZED: _invalid_credentials,
    HTTPStatus.FORBIDDEN: _forbidden,
    HTTPStatus.NOT_FOUND: _missing,
    HTTPStatus.UNPROCESSABLE_ENTITY: _invalid_query,
    HTTPStatus.SERVICE_UNAVAILABLE: _unavailable,
    HTTPStatus.BAD_GATEWAY: _unavailable,
    HTTPStatus.GATEWAY_TIMEOUT: _unavailable,
    HTTPStatus.TOO_MANY_REQUESTS: _unavailable,
}


def _classify_status(response: httpx.Response) -> QuantToolError:
    """Map a non-200 Data Plane reply to a classified error that states why it failed."""
    problem = _Problem(response)
    handler = _STATUS_HANDLERS.get(response.status_code)
    if handler is not None:
        return handler(problem)
    reported = (
        f" ({problem.code})" if problem.code and re.fullmatch(r"[a-z_]{1,40}", problem.code) else ""
    )
    return ToolUpstreamError(
        f"The Data Plane returned an unexpected HTTP {response.status_code}{reported}.",
        upstream_request_id=problem.request_id,
    )


def _problem(response: httpx.Response) -> tuple[str | None, str | None, str | None]:
    try:
        error = response.json()["error"]
        code, message, request_id = error.get("code"), error.get("message"), error.get("request_id")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None, None
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
        request_id if isinstance(request_id, str) else None,
    )


def _retry_after(response: httpx.Response) -> int:
    try:
        return int(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECONDS))
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS


def _sanitize(message: str | None) -> str | None:
    """Keep a Data Interface validation message without paths, control characters or bulk."""
    if not message:
        return None
    cleaned = _PATH_PATTERN.sub("[path]", _CONTROL_PATTERN.sub(" ", message)).strip()
    return cleaned[:MAX_UPSTREAM_REASON_CHARS] or None
