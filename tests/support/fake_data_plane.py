"""An in-process Data Plane: the real ``stram_data`` HTTP service over the synthetic lake.

It lets every test exercise the genuine HTTP contract (routes, scopes, error shapes, JSON) with no
network. Tests can also script failures at the transport level to prove the error taxonomy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from stram_data.data_interface import (
    DataInterface,
    LatestQuote,
    MarketDataProvenance,
    QuoteValues,
)
from stram_data.service.app import create_app
from stram_data.service.auth import AuthenticationError, Caller

LAKE = Path(__file__).parents[1] / "fixtures/lake"
DATA_HOST = "data.test"
IDP_HOST = "idp.test"
DATA_URL = f"https://{DATA_HOST}"
TOKEN_PATH = "/token"  # noqa: S105 - a URL path, not a secret
TOKEN_URL = f"https://{IDP_HOST}{TOKEN_PATH}"
GOOD_TOKEN_PREFIX = "service-token-"  # noqa: S105 - a test fixture value
READ_SCOPES = frozenset(
    {"series:read", "derived:read", "analytics:read", "catalog:read", "lineage:read", "market:iex"}
)
HINDSIGHT = "hindsight:read"

Override = Callable[[httpx.Request], httpx.Response | None]


class _StubVerifier:
    """Accepts tokens this fake issued; grants exactly the scopes it was configured with."""

    def __init__(self, plane: FakeDataPlane) -> None:
        self._plane = plane

    def verify(self, token: str) -> Caller:
        if not token.startswith(GOOD_TOKEN_PREFIX) or token in self._plane.revoked:
            raise AuthenticationError("token rejected")
        scopes = set(self._plane.granted_scopes)
        if token.endswith("-hindsight"):
            scopes.add(HINDSIGHT)
        return Caller(subject="quant-mcp", scopes=frozenset(scopes))


class FakeDataPlane(httpx.AsyncBaseTransport):
    """Routes the token endpoint and the Data Plane to in-process fakes, recording requests."""

    def __init__(self, lake: Path = LAKE) -> None:
        self.granted_scopes: set[str] = set(READ_SCOPES)
        self.revoked: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict[str, str]] = []
        self.token_status = 200
        self.token_lifetime = 300
        self.overrides: list[Override] = []
        self._issued = 0
        self._app = create_app(DataInterface(lake), _StubVerifier(self))  # type: ignore[arg-type]
        self._asgi = httpx.ASGITransport(app=self._app)

    def script(self, override: Override) -> None:
        """Run ``override`` first; a returned response replaces the real answer."""

        self.overrides.append(override)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for override in self.overrides:
            response = override(request)
            if response is not None:
                return response
        if request.url.path == TOKEN_PATH:
            return self._token(request)
        if request.url.path == "/v1/market/quotes" and request.method == "POST":
            return self._latest_quotes(request)
        return await self._asgi.handle_async_request(request)

    def _latest_quotes(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        symbols = body.get("symbols", [])
        now = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
        quotes = tuple(
            LatestQuote(
                status="ok",
                symbol=symbol,
                feed="iex",
                source="alpaca",
                reason=None,
                market_data=MarketDataProvenance(
                    source="alpaca", feed="iex", coverage="single_exchange",
                    is_consolidated=False, event_time=now, received_at=now, age_ms=0,
                ),
                requested_max_age_ms=body.get("max_age_ms", 15_000),
                last_observed_at=now,
                retryable=False,
                next_action="none",
                quote=QuoteValues(
                    bid=Decimal("100.00"), ask=Decimal("100.02"), bid_size=Decimal("10"),
                    ask_size=Decimal("12"), bid_exchange="V", ask_exchange="V", conditions=(),
                ),
                last_quote=None,
            )
            for symbol in symbols
        )
        content = json.dumps([quote.model_dump(mode="json") for quote in quotes])
        return httpx.Response(200, content=content)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(item.split("=", 1) for item in request.content.decode().split("&") if item)
        self.token_requests.append(form)
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_client"})
        self._issued += 1
        suffix = "-hindsight" if "hindsight" in form.get("scope", "") else ""
        token = f"{GOOD_TOKEN_PREFIX}{self._issued}{suffix}"
        return httpx.Response(200, json={"access_token": token, "expires_in": self.token_lifetime})

    def calls_to(self, path: str) -> list[httpx.Request]:
        """The recorded requests whose URL path equals ``path``."""

        return [request for request in self.requests if request.url.path == path]


def problem(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    """A Data Plane style error reply."""

    body = {"error": {"code": code, "message": message, "request_id": "req-test-1"}}
    return httpx.Response(status, content=json.dumps(body), headers=headers)
