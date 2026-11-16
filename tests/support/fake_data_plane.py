"""An in-process Data Plane: the real ``stram_data`` HTTP service over the synthetic lake.

It lets every test exercise the genuine HTTP contract (routes, scopes, error shapes, JSON) with no
network. Tests can also script failures at the transport level to prove the error taxonomy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from stram_data.data_interface import (
    CompanyFundamentals,
    CompanyRatios,
    DataInterface,
    FundamentalMetric,
    FundamentalRatio,
    FundamentalRatiosQuery,
    FundamentalRatiosResult,
    FundamentalsQuery,
    FundamentalsResult,
    FundamentalValue,
    LatestQuote,
    MarketDataBatchQuery,
    MarketDataProvenance,
    QuoteValues,
    RatioInput,
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


class _SyntheticQuoteDataInterface(DataInterface):
    """Use fixture-only stored quotes while exercising the real HTTP route and authorization."""

    def get_latest_quotes(self, query: MarketDataBatchQuery) -> tuple[LatestQuote, ...]:
        return _latest_quotes(query.symbols, query.max_age_ms)

    def get_fundamentals(self, query: FundamentalsQuery) -> FundamentalsResult:
        return _fundamentals(query.symbols, query.as_of or date(2026, 11, 15))

    def get_fundamental_ratios(self, query: FundamentalRatiosQuery) -> FundamentalRatiosResult:
        return _ratios(query.symbols, query.as_of or date(2026, 11, 15), query.groups)


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
        self._app = create_app(_SyntheticQuoteDataInterface(lake), _StubVerifier(self))  # type: ignore[arg-type]
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
        return await self._asgi.handle_async_request(request)

    def quote_response(self, symbols: tuple[str, ...], max_age_ms: int = 15_000) -> httpx.Response:
        """Build a structurally valid quote response for a deliberate transport-level override."""

        quotes = _latest_quotes(symbols, max_age_ms)
        content = json.dumps([quote.model_dump(mode="json") for quote in quotes])
        return httpx.Response(200, content=content)

    def ratios_response(self, symbols: tuple[str, ...]) -> httpx.Response:
        """A structurally valid /v1/fundamentals/ratios reply, for a transport-level override."""

        return httpx.Response(
            200, content=_ratios(symbols, date(2026, 11, 15), ()).model_dump_json()
        )

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


# A ratio with many digits: it must reach the caller exactly, never through a float.
EXACT_RATIO = Decimal("0.6086956521739130434782608696")


def _ratios(
    symbols: tuple[str, ...], as_of: date, groups: tuple[str, ...]
) -> FundamentalRatiosResult:
    """Fixture ratios: AAPL has one worked out and one null with its reason; others are unknown."""

    margin_inputs = (
        RatioInput(
            name="gross_profit",
            value=Decimal("700"),
            period_end=date(2026, 6, 30),
            source="derived",
            caveat="gross profit is derived as revenue - cost_of_revenue (not reported)",
        ),
        RatioInput(
            name="revenue",
            value=Decimal("1150"),
            period_end=date(2026, 6, 30),
            source="us-gaap:Revenues",
            caveat=None,
        ),
    )
    ratios = (
        FundamentalRatio(
            name="gross_margin",
            group="profitability",
            value=EXACT_RATIO,
            basis="ttm",
            formula="gross_profit / revenue",
            inputs=margin_inputs,
            period_end=date(2026, 6, 30),
            note=margin_inputs[0].caveat,
        ),
        FundamentalRatio(
            name="current_ratio",
            group="liquidity",
            value=None,
            basis="latest_balance",
            formula="current_assets / current_liabilities",
            inputs=(),
            period_end=None,
            note="missing: current_assets, current_liabilities",
        ),
    )
    kept = tuple(ratio for ratio in ratios if not groups or ratio.group in groups)

    def company(symbol: str) -> CompanyRatios:
        known = symbol == "AAPL"
        return CompanyRatios(
            symbol=symbol,
            cik="0000320193" if known else None,
            name="Apple Inc." if known else None,
            price=None,
            price_date=None,
            ratios=kept if known else (),
            unavailable=None if known else "not a tracked company with SEC filings",
        )

    return FundamentalRatiosResult(
        as_of=as_of,
        companies=tuple(company(symbol) for symbol in symbols),
        availability_note="Ratios are worked out from reported XBRL facts.",
    )


def _fundamentals(symbols: tuple[str, ...], as_of: date) -> FundamentalsResult:
    """Fixture fundamentals: AAPL's FY2026 revenue and EPS; any other symbol is unknown."""

    def value(amount: str) -> FundamentalValue:
        return FundamentalValue(
            value=Decimal(amount),
            period_start=date(2025, 9, 28),
            period_end=date(2026, 9, 26),
            accessions=("0000320193-26-000026",),
            filed=date(2026, 11, 3),
            method="reported",
        )

    def company(symbol: str) -> CompanyFundamentals:
        if symbol != "AAPL":
            return CompanyFundamentals(
                symbol=symbol,
                cik=None,
                name=None,
                metrics=(),
                price=None,
                price_date=None,
                pe_ttm=None,
                unavailable="not a tracked company with SEC filings",
            )
        metrics = (
            FundamentalMetric(
                metric="revenue",
                taxonomy="us-gaap",
                concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                unit="USD",
                latest=value("478834000000"),
                ttm=value("478834000000"),
            ),
            FundamentalMetric(
                metric="eps_diluted",
                taxonomy="us-gaap",
                concept="EarningsPerShareDiluted",
                unit="USD/shares",
                latest=value("8.98"),
                ttm=value("8.98"),
            ),
        )
        return CompanyFundamentals(
            symbol=symbol,
            cik="0000320193",
            name="Apple Inc.",
            metrics=metrics,
            price=Decimal("356.40"),
            price_date=date(2026, 11, 13),
            pe_ttm=Decimal("39.69"),
            unavailable=None,
        )

    return FundamentalsResult(
        as_of=as_of,
        companies=tuple(company(symbol) for symbol in symbols),
        availability_note="Values are as reported to the SEC in XBRL.",
    )


def _latest_quotes(symbols: tuple[str, ...], max_age_ms: int) -> tuple[LatestQuote, ...]:
    """Fixture rows representing stored IEX data for the Data Interface test double."""

    now = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
    return tuple(
        LatestQuote(
            status="ok",
            symbol=symbol,
            feed="iex",
            source="alpaca",
            reason=None,
            market_data=MarketDataProvenance(
                source="alpaca",
                feed="iex",
                coverage="single_exchange",
                is_consolidated=False,
                event_time=now,
                received_at=now,
                age_ms=0,
            ),
            requested_max_age_ms=max_age_ms,
            last_observed_at=now,
            retryable=False,
            next_action="none",
            quote=QuoteValues(
                bid=Decimal("100.00"),
                ask=Decimal("100.02"),
                bid_size=Decimal("10"),
                ask_size=Decimal("12"),
                bid_exchange="V",
                ask_exchange="V",
                conditions=(),
            ),
            last_quote=None,
        )
        for symbol in symbols
    )


def problem(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    """A Data Plane style error reply."""

    body = {"error": {"code": code, "message": message, "request_id": "req-test-1"}}
    return httpx.Response(status, content=json.dumps(body), headers=headers)
