"""Typed read-only access to the Data Plane: one method per reviewed route.

Request bodies are the Data Interface's own query models. Every response is parsed into the
matching result model, so an upstream reply that drifts from the reviewed contract fails here as a
named, non-retryable ``data_plane_error`` instead of reaching a client as malformed data.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final
from urllib.parse import quote

from pydantic import BaseModel, TypeAdapter, ValidationError
from stram_data.data_interface import (
    CatalogEntry,
    ConditionalReturnsQuery,
    ConditionalReturnsResult,
    DerivedQuery,
    DerivedResult,
    FreshnessReport,
    IndicatorInfo,
    LatestQuote,
    LineageResult,
    MarketDataBatchQuery,
    PanelQuery,
    PanelResult,
    PolicyInfo,
    SeriesQuery,
    SeriesResult,
)

from stram_quant_mcp.data_client import HINDSIGHT_SCOPE, DataPlaneClient
from stram_quant_mcp.errors import ToolUpstreamError

_CATALOG: Final = TypeAdapter(tuple[CatalogEntry, ...])
_INDICATORS: Final = TypeAdapter(tuple[IndicatorInfo, ...])
_POLICIES: Final = TypeAdapter(tuple[PolicyInfo, ...])
_SUBJECTS: Final = TypeAdapter(tuple[str, ...])
_LATEST_QUOTES: Final = TypeAdapter(tuple[LatestQuote, ...])
_INTERFACE_PATTERN: Final = re.compile(r"Data Interface v(\d+)")
_VERSION_PATTERN: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
MAX_REPORTED_FIELDS: Final = 3


class DataGateway:
    """The Data Interface as the Data Plane serves it over HTTP."""

    def __init__(self, client: DataPlaneClient) -> None:
        """Serve the routes through ``client``."""
        self._client = client

    async def service_versions(self) -> tuple[str, str]:
        """Return ``(service_version, interface_major)`` from the public OpenAPI document."""
        raw = await self._client.get_public("/v1/openapi.json")
        try:
            document = json.loads(raw)
            info = document["info"]
            version = str(info["version"])
            interface = _INTERFACE_PATTERN.search(str(info["description"]))
        except (ValueError, KeyError, TypeError):
            raise ToolUpstreamError(
                "The Data Plane OpenAPI document is malformed, so its version is unknown."
            ) from None
        if interface is None or _VERSION_PATTERN.match(version) is None:
            raise ToolUpstreamError(
                "The Data Plane OpenAPI document does not state its service and interface versions."
            )
        return version, interface.group(1)

    async def get_catalog(
        self, *, dataset: str | None, limit: int, deadline_seconds: float
    ) -> tuple[CatalogEntry, ...]:
        """List catalog entries (GET /v1/catalog)."""
        params: dict[str, str | int] = {"limit": limit}
        if dataset is not None:
            params["dataset"] = dataset
        raw = await self._client.get_json(
            "/v1/catalog", params=params, timeout_seconds=deadline_seconds
        )
        return _parse(_CATALOG, raw)

    async def list_indicators(self, *, deadline_seconds: float) -> tuple[IndicatorInfo, ...]:
        """List derived indicators (GET /v1/indicators)."""
        raw = await self._client.get_json("/v1/indicators", timeout_seconds=deadline_seconds)
        return _parse(_INDICATORS, raw)

    async def list_indicator_subjects(
        self, indicator_id: str, *, deadline_seconds: float
    ) -> tuple[str, ...]:
        """List the subjects of one indicator (GET /v1/indicators/{id}/subjects)."""
        path = f"/v1/indicators/{quote(indicator_id, safe='')}/subjects"
        raw = await self._client.get_json(path, timeout_seconds=deadline_seconds)
        return _parse(_SUBJECTS, raw)

    async def describe_availability_policies(
        self, *, deadline_seconds: float
    ) -> tuple[PolicyInfo, ...]:
        """Describe the availability policies in force (GET /v1/policies)."""
        raw = await self._client.get_json("/v1/policies", timeout_seconds=deadline_seconds)
        return _parse(_POLICIES, raw)

    async def get_freshness(self, *, deadline_seconds: float) -> FreshnessReport:
        """Report dataset freshness (GET /v1/freshness)."""
        raw = await self._client.get_json("/v1/freshness", timeout_seconds=deadline_seconds)
        return _parse(FreshnessReport, raw)

    async def get_lineage(self, event_id: str, *, deadline_seconds: float) -> LineageResult:
        """Resolve the source observations behind one event (GET /v1/lineage/{id})."""
        path = f"/v1/lineage/{quote(event_id, safe='')}"
        raw = await self._client.get_json(path, timeout_seconds=deadline_seconds)
        return _parse(LineageResult, raw)

    async def get_series(self, query: SeriesQuery, *, deadline_seconds: float) -> SeriesResult:
        """Query series (POST /v1/series/query)."""
        raw = await self._post("/v1/series/query", query, deadline_seconds)
        return _parse(SeriesResult, raw)

    async def get_derived(self, query: DerivedQuery, *, deadline_seconds: float) -> DerivedResult:
        """Query one derived indicator (POST /v1/derived/query)."""
        raw = await self._post("/v1/derived/query", query, deadline_seconds)
        return _parse(DerivedResult, raw)

    async def get_aligned_panel(self, query: PanelQuery, *, deadline_seconds: float) -> PanelResult:
        """Query an aligned panel (POST /v1/panel/query)."""
        raw = await self._post("/v1/panel/query", query, deadline_seconds)
        return _parse(PanelResult, raw)

    async def get_conditional_returns(
        self, query: ConditionalReturnsQuery, *, deadline_seconds: float
    ) -> ConditionalReturnsResult:
        """Query conditional returns (POST /v1/conditional-returns/query)."""
        raw = await self._post("/v1/conditional-returns/query", query, deadline_seconds)
        return _parse(ConditionalReturnsResult, raw)

    async def get_latest_quotes(
        self, query: MarketDataBatchQuery, *, deadline_seconds: float
    ) -> tuple[LatestQuote, ...]:
        """Read bounded IEX top-of-book results (POST /v1/market/quotes)."""
        raw = await self._post("/v1/market/quotes", query, deadline_seconds)
        return _parse(_LATEST_QUOTES, raw)

    async def _post(self, path: str, query: BaseModel, deadline_seconds: float) -> bytes:
        # Hindsight reads need an extra, separately requested scope; nothing else does.
        scope = HINDSIGHT_SCOPE if getattr(query, "availability", None) == "ex_post" else None
        return await self._client.post_json(
            path,
            query.model_dump(mode="json"),
            timeout_seconds=deadline_seconds,
            extra_scope=scope,
        )


def _parse[T](model: type[T] | TypeAdapter[T], raw: bytes) -> T:
    """Validate an upstream body against the reviewed model; drift is a named failure."""
    adapter: Any = model if isinstance(model, TypeAdapter) else TypeAdapter(model)
    try:
        parsed: T = adapter.validate_json(raw)
    except ValidationError as error:
        fields = {".".join(str(part) for part in item["loc"][:3]) for item in error.errors()}
        where = ", ".join(sorted(fields)[:MAX_REPORTED_FIELDS]) or "body"
        raise ToolUpstreamError(
            "The Data Plane response does not match the reviewed contract "
            f"(invalid fields near: {where}). The service may be running an incompatible version."
        ) from None
    return parsed
