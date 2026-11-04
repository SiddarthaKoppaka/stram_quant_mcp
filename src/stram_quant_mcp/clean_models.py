"""Closed models for the clean view: the raw facts, in a shape sized for an agent.

A clean result holds no value the raw result lacks. Fields shared by every row of a series appear
once; rows become short tuples named by ``columns``. Dates are ISO strings and exact decimals are
strings without trailing zeros.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from stram_quant_mcp.contract_base import AvailabilityMode, ContractModel

Cell = str | float | int | bool | None
Row = tuple[Cell, ...]
CleanMarker = Literal["clean"]


class CleanAvailability(ContractModel):
    """The availability summary, without the policy texts the discovery tools already give."""

    mode: AvailabilityMode
    hindsight_only: bool
    unavailable_rows: int
    uncovered: tuple[str, ...]
    warnings: tuple[str, ...]


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


class CleanSeries(ContractModel):
    """One series, with its shared fields once and a row per observation."""

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
    availability: CleanAvailability
    explanation: str | None = None


class CleanDerivedGroup(ContractModel):
    """One derived indicator for one subject, with its definition once."""

    indicator_id: str
    indicator_version: str
    subject: str | None
    unit: str
    formula: str | None
    availability_basis: str | None
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


class CleanDerivedData(ContractModel):
    """Derived rows grouped by indicator and subject; ``event_id`` resolves through lineage."""

    view: CleanMarker
    groups: tuple[CleanDerivedGroup, ...]
    count: int
    limit: int
    truncated: bool
    as_of: str | None
    availability_note: str
    availability: CleanAvailability
    explanation: str | None = None


class CleanPanelColumn(ContractModel):
    """What the cells of one panel column hold, without repeating it in every cell."""

    name: str
    missing_cells: int
    filled_cells: int
    stale_cells: int
    known_since_first: str | None
    known_since_last: str | None


class CleanPanelData(ContractModel):
    """The aligned panel as a table: one row per date, one value per column."""

    view: CleanMarker
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    column_details: tuple[CleanPanelColumn, ...]
    filled_or_stale: tuple[Row, ...] = Field(default=())
    count: int
    limit: int
    truncated: bool
    as_of: str | None
    calendar: str
    fill: str
    availability_note: str
    availability: CleanAvailability
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
    events: tuple[Row, ...]
    truncated: bool
    notes: tuple[str, ...]
    availability_note: str
    availability: CleanAvailability
    explanation: str | None = None
