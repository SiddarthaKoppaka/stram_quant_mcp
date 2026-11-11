"""Thin delegation from validated tool contracts to the public Data Interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import BaseModel, ValidationError
from stram_data.data_interface import (
    INTERFACE_VERSION,
    Condition,
    ConditionalReturnsQuery,
    ConditionalReturnsResult,
    DerivedQuery,
    DerivedResult,
    MarketDataBatchQuery,
    PanelColumn,
    PanelQuery,
    PanelResult,
    SeriesQuery,
    SeriesResult,
)

from stram_quant_mcp.clean_views import (
    clean_catalog,
    clean_conditional_returns,
    clean_derived,
    clean_panel,
    clean_series,
)
from stram_quant_mcp.data_gateway import DataGateway
from stram_quant_mcp.errors import ToolInputError, ToolUpstreamError
from stram_quant_mcp.input_validation import describe_validation_error
from stram_quant_mcp.tool_models import (
    AvailabilityPoliciesData,
    CatalogData,
    CatalogInput,
    ConditionalReturnsInput,
    DerivedInput,
    IndicatorsData,
    IndicatorSubjectsData,
    IndicatorSubjectsInput,
    LatestQuotesData,
    LatestQuotesInput,
    LineageInput,
    PanelInput,
    SeriesInput,
)
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME
from stram_quant_mcp.version import __version__

Handler = Callable[[BaseModel, float], Awaitable[BaseModel]]


class QuantToolAdapter:
    """Execute reviewed tool calls over the Data Plane without adding quantitative behavior."""

    def __init__(self, gateway: DataGateway, *, policy_version: str, service_version: str) -> None:
        """Serve tools through ``gateway``, labelling envelopes with the versions given."""
        self._gateway = gateway
        self._policy_version = policy_version
        self._service_version = service_version

    @property
    def policy_version(self) -> str:
        return self._policy_version

    async def execute(
        self, tool_name: str, arguments: object, request_id: str, deadline_seconds: float
    ) -> BaseModel:
        """Validate the input, call the Data Plane, and validate the complete envelope.

        Raises:
            ToolInputError: The arguments, or the Data Plane, rejected the request; the reason
                names the field and rule.
            QuantToolError: Any classified Data Plane failure, unchanged.
        """
        spec = TOOLS_BY_NAME.get(tool_name)
        if spec is None:
            raise ToolInputError(f"{tool_name[:60]!r} is not a tool of this service.") from None
        try:
            request = spec.input_model.model_validate(arguments)
        except ValidationError as error:
            raise ToolInputError(describe_validation_error(error)) from None
        try:
            data = await self._dispatch(tool_name, request, deadline_seconds)
        except ValidationError as error:
            raise ToolInputError(describe_validation_error(error)) from None
        # Warnings come from the raw result, so both views carry the same ones.
        warnings = _warnings(data)
        try:
            if getattr(request, "view", "raw") == "clean":
                data = _clean(data, request)
            return spec.output_model.model_validate(
                {
                    "tool_version": __version__,
                    "stram_data_version": self._service_version,
                    "interface_version": INTERFACE_VERSION,
                    "policy_version": self._policy_version,
                    "request_id": request_id,
                    "data": data,
                    "warnings": warnings,
                }
            )
        except ValidationError as error:
            raise ToolUpstreamError(
                "The result could not be shaped into the reviewed output schema "
                f"({describe_validation_error(error)})."
            ) from None

    async def _dispatch(
        self, tool_name: str, request: BaseModel, deadline_seconds: float
    ) -> BaseModel:
        handler = self._handlers().get(tool_name)
        if handler is None:
            raise ToolInputError(f"{tool_name[:60]!r} has no Data Plane route.") from None
        return await handler(request, deadline_seconds)

    def _handlers(self) -> dict[str, Handler]:
        return {
            "quant_list_series": self._catalog,
            "quant_list_indicators": self._indicators,
            "quant_list_indicator_subjects": self._subjects,
            "quant_describe_availability": self._policies,
            "quant_get_freshness": self._freshness,
            "quant_get_series": self._series,
            "quant_get_derived": self._derived,
            "quant_get_lineage": self._lineage,
            "quant_get_aligned_panel": self._panel,
            "quant_get_conditional_returns": self._conditional_returns,
            "quant_get_latest_quotes": self._latest_quotes,
        }

    async def _catalog(self, request: BaseModel, deadline: float) -> BaseModel:
        catalog = cast(CatalogInput, request)
        entries = await self._gateway.get_catalog(
            dataset=catalog.dataset, limit=catalog.limit, deadline_seconds=deadline
        )
        return CatalogData(entries=entries)

    async def _indicators(self, _request: BaseModel, deadline: float) -> BaseModel:
        return IndicatorsData(
            indicators=await self._gateway.list_indicators(deadline_seconds=deadline)
        )

    async def _subjects(self, request: BaseModel, deadline: float) -> BaseModel:
        indicator = cast(IndicatorSubjectsInput, request).indicator_id
        found = await self._gateway.list_indicator_subjects(indicator, deadline_seconds=deadline)
        return IndicatorSubjectsData(subjects=found)

    async def _policies(self, _request: BaseModel, deadline: float) -> BaseModel:
        policies = await self._gateway.describe_availability_policies(deadline_seconds=deadline)
        return AvailabilityPoliciesData(policies=policies)

    async def _freshness(self, _request: BaseModel, deadline: float) -> BaseModel:
        return await self._gateway.get_freshness(deadline_seconds=deadline)

    async def _series(self, request: BaseModel, deadline: float) -> BaseModel:
        series = cast(SeriesInput, request)
        query = SeriesQuery.model_validate(series.model_dump(exclude={"view"}))
        return await self._gateway.get_series(query, deadline_seconds=deadline)

    async def _derived(self, request: BaseModel, deadline: float) -> BaseModel:
        derived = cast(DerivedInput, request)
        query = DerivedQuery.model_validate(derived.model_dump(exclude={"view"}))
        return await self._gateway.get_derived(query, deadline_seconds=deadline)

    async def _lineage(self, request: BaseModel, deadline: float) -> BaseModel:
        event_id = str(cast(LineageInput, request).event_id)
        return await self._gateway.get_lineage(event_id, deadline_seconds=deadline)

    async def _panel(self, request: BaseModel, deadline: float) -> BaseModel:
        query = _panel_query(cast(PanelInput, request))
        return await self._gateway.get_aligned_panel(query, deadline_seconds=deadline)

    async def _conditional_returns(self, request: BaseModel, deadline: float) -> BaseModel:
        query = _conditional_query(cast(ConditionalReturnsInput, request))
        return await self._gateway.get_conditional_returns(query, deadline_seconds=deadline)

    async def _latest_quotes(self, request: BaseModel, deadline: float) -> BaseModel:
        latest = cast(LatestQuotesInput, request)
        quotes = await self._gateway.get_latest_quotes(
            MarketDataBatchQuery(symbols=latest.symbols, max_age_ms=latest.max_age_ms),
            deadline_seconds=deadline,
        )
        return LatestQuotesData(quotes=quotes)


def _panel_query(request: PanelInput) -> PanelQuery:
    payload = request.model_dump(exclude={"columns", "view"})
    payload["columns"] = tuple(
        PanelColumn.model_validate(column.model_dump()) for column in request.columns
    )
    return PanelQuery.model_validate(payload)


def _conditional_query(request: ConditionalReturnsInput) -> ConditionalReturnsQuery:
    payload = request.model_dump(exclude={"conditions", "view"})
    payload["conditions"] = tuple(
        Condition.model_validate(condition.model_dump()) for condition in request.conditions
    )
    return ConditionalReturnsQuery.model_validate(payload)


def _clean(data: BaseModel, request: BaseModel) -> BaseModel:
    """Reshape a raw result into its clean view; values are copied, never computed."""
    if isinstance(data, CatalogData):
        return clean_catalog(data, cast(CatalogInput, request).limit)
    if isinstance(data, SeriesResult):
        return clean_series(data)
    if isinstance(data, DerivedResult):
        return clean_derived(data)
    if isinstance(data, PanelResult):
        return clean_panel(data)
    if isinstance(data, ConditionalReturnsResult):
        return clean_conditional_returns(data)
    raise ToolInputError("This tool has no clean view; use view='raw'.") from None


def _warnings(data: BaseModel) -> tuple[str, ...]:
    warnings: list[str] = []
    truncated = getattr(data, "truncated", False)
    if truncated:
        warnings.append("Result is truncated; narrow the date range or filters.")
    availability = getattr(data, "availability", None)
    if availability is not None:
        if availability.hindsight_only:
            warnings.append("Result uses ex_post availability and is hindsight-only.")
        warnings.extend(availability.warnings)
        if availability.uncovered:
            warnings.append("Some rows are outside covered availability policies.")
    return tuple(dict.fromkeys(warnings))
