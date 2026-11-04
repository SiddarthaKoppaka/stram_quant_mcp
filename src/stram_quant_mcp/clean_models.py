"""Closed models for the clean view: every raw value, in a shape sized for an agent.

The clean view is lossless. Fields that every row of a group shares are stated once in the group;
everything else, including identifiers, timestamps, lineage and availability evidence, stays on
each row. Rows are short tuples named by ``columns``. Dates and timestamps are ISO strings and exact
decimals are strings without trailing zeros.
"""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue
from stram_data.data_interface import AvailabilitySummary

from stram_quant_mcp.contract_base import ContractModel

Row = tuple[JsonValue, ...]
CleanMarker = Literal["clean"]


class CleanCatalogData(ContractModel):
    """Series metadata as one table, with counts by dataset and provider."""

    view: CleanMarker
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    count: int
    limit: int
    limit_reached: bool
    by_dataset: dict[str, int]
    by_provider: dict[str, int]
    explanation: str | None = None


class CleanSeries(ContractModel):
    """One series, with the fields its rows share stated once."""

    series_key: str
    dataset: str
    kind: str
    provider: str
    unit: str
    indicator_id: str | None
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


class CleanSeriesData(ContractModel):
    """Series rows grouped by series."""

    view: CleanMarker
    series: tuple[CleanSeries, ...]
    count: int
    limit: int
    truncated: bool
    as_of: str | None
    all_versions: bool
    availability_note: str
    availability: AvailabilitySummary
    explanation: str | None = None


class CleanDerivedGroup(ContractModel):
    """One derived indicator definition for one subject, with its rows."""

    indicator_id: str
    indicator_version: str
    subject: str | None
    unit: str
    formula: str | None
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


class CleanDerivedData(ContractModel):
    """Derived rows grouped by indicator, subject, version, unit and formula."""

    view: CleanMarker
    groups: tuple[CleanDerivedGroup, ...]
    count: int
    limit: int
    truncated: bool
    as_of: str | None
    availability_note: str
    availability: AvailabilitySummary
    explanation: str | None = None


class CleanPanelColumn(ContractModel):
    """Counts over one panel column's cells, read from the cells themselves."""

    name: str
    absent_cells: int
    null_values: int
    filled_cells: int
    stale_cells: int


class CleanPanelData(ContractModel):
    """The aligned panel as a value table, plus every present cell's details.

    A cell missing from ``cells`` was absent in the raw panel; a listed cell with a null value was
    present with no value. The two are never merged.
    """

    view: CleanMarker
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    cell_columns: tuple[str, ...]
    cells: tuple[Row, ...]
    column_details: tuple[CleanPanelColumn, ...]
    count: int
    limit: int
    truncated: bool
    as_of: str | None
    calendar: str
    fill: str
    availability_note: str
    availability: AvailabilitySummary
    explanation: str | None = None


class CleanHorizon(ContractModel):
    """The conditional outcome and its baseline for one horizon, side by side."""

    horizon: int
    observations: int
    mean: float | None
    median: float | None
    hit_rate: float | None
    minimum: float | None
    maximum: float | None
    baseline_observations: int | None
    baseline_mean: float | None
    baseline_median: float | None
    baseline_hit_rate: float | None
    baseline_minimum: float | None
    baseline_maximum: float | None


class CleanConditionalReturnsData(ContractModel):
    """A conditional-returns study with outcomes and baseline merged per horizon."""

    view: CleanMarker
    target_symbol: str
    trigger: str
    entry: str
    as_of: str | None
    evaluated_days: int
    undetermined_days: int
    state_days: int
    event_count: int
    horizons: tuple[CleanHorizon, ...]
    event_columns: tuple[str, ...]
    condition_columns: tuple[str, ...]
    events: tuple[Row, ...]
    truncated: bool
    notes: tuple[str, ...]
    availability_note: str
    availability: AvailabilitySummary
    explanation: str | None = None
