"""Thin delegation from validated tool contracts to the public Data Interface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import cast

import stram_data
from pydantic import BaseModel, ValidationError
from stram_data.data_interface import (
    INTERFACE_VERSION,
    Condition,
    ConditionalReturnsQuery,
    ConditionalReturnsResult,
    DataInterface,
    DataUnavailableError,
    DerivedQuery,
    DerivedResult,
    InvalidQueryError,
    NotFoundError,
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
from stram_quant_mcp.errors import ToolInputError, ToolNotFoundError, ToolUnavailableError
from stram_quant_mcp.tool_models import (
    AvailabilityPoliciesData,
    CatalogData,
    CatalogInput,
    ConditionalReturnsInput,
    DerivedInput,
    IndicatorsData,
    IndicatorSubjectsData,
    IndicatorSubjectsInput,
    LineageInput,
    PanelInput,
    SeriesInput,
)
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME
from stram_quant_mcp.version import __version__


class QuantToolAdapter:
    """Execute reviewed tool calls without adding quantitative behavior."""

    def __init__(self, data: DataInterface) -> None:
        self._data = data
        descriptions = [
            item.model_dump(mode="json") for item in data.describe_availability_policies()
        ]
        encoded = json.dumps(descriptions, sort_keys=True, separators=(",", ":")).encode()
        self._policy_version = hashlib.sha256(encoded).hexdigest()[:16]

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def execute(self, tool_name: str, arguments: object, request_id: str) -> BaseModel:
        spec = TOOLS_BY_NAME.get(tool_name)
        if spec is None:
            raise ToolInputError from None
        try:
            request = spec.input_model.model_validate(arguments)
            # The view selects the response shape only; the Data Interface never sees it.
            data = self._dispatch(tool_name, request)
            # Warnings come from the raw result, so both views carry the same ones.
            warnings = _warnings(data)
            if getattr(request, "view", "raw") == "clean":
                data = _clean(data, request)
            envelope = spec.output_model.model_validate(
                {
                    "tool_version": __version__,
                    "stram_data_version": stram_data.__version__,
                    "interface_version": INTERFACE_VERSION,
                    "policy_version": self._policy_version,
                    "request_id": request_id,
                    "data": data,
                    "warnings": warnings,
                }
            )
        except (ValidationError, InvalidQueryError) as error:
            raise ToolInputError from error
        except NotFoundError as error:
            raise ToolNotFoundError from error
        except DataUnavailableError as error:
            raise ToolUnavailableError from error
        return envelope

    def _dispatch(self, tool_name: str, request: BaseModel) -> BaseModel:
        dispatch: dict[str, Callable[[], BaseModel]] = {
            "quant_list_series": lambda: self._catalog(cast(CatalogInput, request)),
            "quant_list_indicators": lambda: IndicatorsData(
                indicators=self._data.list_indicators()
            ),
            "quant_list_indicator_subjects": lambda: self._subjects(
                cast(IndicatorSubjectsInput, request)
            ),
            "quant_describe_availability": lambda: AvailabilityPoliciesData(
                policies=self._data.describe_availability_policies()
            ),
            "quant_get_freshness": self._data.get_freshness,
            "quant_get_series": lambda: self._series(cast(SeriesInput, request)),
            "quant_get_derived": lambda: self._derived(cast(DerivedInput, request)),
            "quant_get_lineage": lambda: self._data.get_lineage(
                str(cast(LineageInput, request).event_id)
            ),
            "quant_get_aligned_panel": lambda: self._panel(cast(PanelInput, request)),
            "quant_get_conditional_returns": lambda: self._conditional_returns(
                cast(ConditionalReturnsInput, request)
            ),
        }
        try:
            operation = dispatch[tool_name]
        except KeyError as error:
            raise ToolInputError from error
        return operation()

    def _catalog(self, request: CatalogInput) -> CatalogData:
        return CatalogData(
            entries=self._data.get_catalog(dataset=request.dataset, limit=request.limit)
        )

    def _subjects(self, request: IndicatorSubjectsInput) -> IndicatorSubjectsData:
        return IndicatorSubjectsData(
            subjects=self._data.list_indicator_subjects(request.indicator_id)
        )

    def _series(self, request: SeriesInput) -> SeriesResult:
        return self._data.get_series(
            SeriesQuery.model_validate(request.model_dump(exclude={"view"}))
        )

    def _derived(self, request: DerivedInput) -> DerivedResult:
        return self._data.get_derived(
            DerivedQuery.model_validate(request.model_dump(exclude={"view"}))
        )

    def _panel(self, request: PanelInput) -> PanelResult:
        payload = request.model_dump(exclude={"columns", "view"})
        payload["columns"] = tuple(
            PanelColumn.model_validate(column.model_dump()) for column in request.columns
        )
        return self._data.get_aligned_panel(PanelQuery.model_validate(payload))

    def _conditional_returns(self, request: ConditionalReturnsInput) -> ConditionalReturnsResult:
        payload = request.model_dump(exclude={"conditions", "view"})
        payload["conditions"] = tuple(
            Condition.model_validate(condition.model_dump()) for condition in request.conditions
        )
        return self._data.get_conditional_returns(ConditionalReturnsQuery.model_validate(payload))


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
    raise ToolInputError from None


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
