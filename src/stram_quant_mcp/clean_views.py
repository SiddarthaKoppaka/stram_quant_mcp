"""Reshape raw Data Interface results into the lossless clean view.

These functions only regroup and rename. Every raw value is kept: fields a whole group shares are
stated once in the group, and every other field stays on its row, serialized exactly as the raw
view serializes it. The one change of form is that exact decimals lose trailing zeros as text, never
by rounding. Nothing is computed except counts of the rows being returned.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal

from pydantic import BaseModel, JsonValue
from stram_data.data_interface import (
    CatalogEntry,
    ConditionalEvent,
    ConditionalReturnsResult,
    DerivedResult,
    DerivedRow,
    HorizonSummary,
    PanelResult,
    SeriesResult,
    SeriesRow,
)

from stram_quant_mcp.tool_models import (
    CatalogData,
    CleanCatalogData,
    CleanConditionalReturnsData,
    CleanDerivedData,
    CleanDerivedGroup,
    CleanHorizon,
    CleanPanelColumn,
    CleanPanelData,
    CleanSeries,
    CleanSeriesData,
    Row,
)

# Spellings of one frequency seen across providers. The raw spelling is always kept beside it.
FREQUENCIES = {
    "d": "daily",
    "day": "daily",
    "daily": "daily",
    "w": "weekly",
    "week": "weekly",
    "weekly": "weekly",
    "m": "monthly",
    "month": "monthly",
    "monthly": "monthly",
    "q": "quarterly",
    "quarter": "quarterly",
    "quarterly": "quarterly",
    "a": "annual",
    "annual": "annual",
}
CATALOG_COLUMNS = (
    "dataset",
    "kind",
    "provider",
    "series_key",
    "name",
    "unit",
    "frequency",
    "frequency_normalized",
    "record_count",
    "first_observation",
    "last_observation",
)
SERIES_GROUP = ("dataset", "kind", "provider", "series_key", "indicator_id", "unit")
SERIES_COLUMNS = (
    "observed_on",
    "observed_at",
    "value",
    "known_since",
    "is_vintage",
    "ingested_at",
    "event_id",
    "record_id",
    "quality_flags",
    "availability",
)
DERIVED_GROUP = ("indicator_id", "indicator_version", "subject", "unit", "formula")
DERIVED_COLUMNS = (
    "observation_date",
    "value",
    "known_since",
    "computed_at",
    "event_id",
    "input_event_ids",
    "input_batch_ids",
    "availability_basis",
    "licensed_input",
    "retracted",
    "availability",
)
CELL_COLUMNS = (
    "observed_on",
    "column",
    "value",
    "cell_observed_on",
    "known_since",
    "filled",
    "stale_days",
    "availability",
)
EVENT_COLUMNS = (
    "event_date",
    "entry_date",
    "entry_close",
    "forward_returns",
    "signal_cutoff_at",
    "signal_available_at",
    "entry_at",
    "conditions",
)
CONDITION_COLUMNS = (
    "series_key",
    "indicator_id",
    "observed_on",
    "value",
    "known_since",
    "source_event_id",
    "previous_observed_on",
    "previous_value",
    "availability",
)
NO_ROWS = (
    "No rows matched in this window and availability mode. A value counts only from its "
    "known_since date, so history loaded recently may not be usable for earlier dates in strict "
    "mode; try availability='estimated', a later window or different filters."
)
NO_CATALOG_ENTRIES = "No series matched; the dataset filter may exclude every entry."
LIMIT_FILLED = (
    "The result filled the requested limit, so more entries may exist; filter by dataset or raise "
    "the limit to see them."
)
NO_EVALUATED_DAYS = (
    "No trading days in the window could be evaluated, so there is nothing to measure."
)
MOSTLY_UNDETERMINED = (
    "Most days in the window were undetermined: the condition's data was not yet known on those "
    "days in this availability mode, so it could not be evaluated. Try availability='estimated' "
    "or a later start_date before reading the statistics."
)
NO_EVENTS = (
    "The condition never held on an evaluated day, so there are no events; the baseline still "
    "shows returns over every day in the window."
)


def exact(value: Decimal | None) -> str | None:
    """Write an exact decimal without trailing zeros, as text, so no digit is ever rounded."""
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def frequency(value: str | None) -> str | None:
    """Return the one word for a frequency spelling, or the spelling itself if unknown."""
    if value is None:
        return None
    return FREQUENCIES.get(value.strip().lower(), value)


def _table(rows: Iterable[BaseModel], columns: Sequence[str]) -> tuple[Row, ...]:
    """Pick ``columns`` from each row's raw JSON form, in order."""
    dumped = (row.model_dump(mode="json") for row in rows)
    return tuple(tuple(item[name] for name in columns) for item in dumped)


def _grouped[RowT](rows: Iterable[RowT], key: Callable[[RowT], object]) -> list[list[RowT]]:
    groups: dict[object, list[RowT]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return list(groups.values())


def clean_catalog(data: CatalogData, limit: int) -> CleanCatalogData:
    """Turn catalog entries into one table with counts by dataset and provider.

    Args:
        data: The raw catalog.
        limit: The row limit of the request, so the result can say when it was reached.
    """
    entries = data.entries
    return CleanCatalogData(
        view="clean",
        columns=CATALOG_COLUMNS,
        rows=tuple(_catalog_row(entry) for entry in entries),
        count=len(entries),
        limit=limit,
        limit_filled=len(entries) >= limit,
        by_dataset=dict(Counter(entry.dataset for entry in entries)),
        by_provider=dict(Counter(entry.provider for entry in entries)),
        explanation=_catalog_explanation(len(entries), limit),
    )


def _catalog_explanation(count: int, limit: int) -> str | None:
    if count == 0:
        return NO_CATALOG_ENTRIES
    return LIMIT_FILLED if count >= limit else None


def _catalog_row(entry: CatalogEntry) -> Row:
    dumped = entry.model_dump(mode="json")
    dumped["frequency_normalized"] = frequency(entry.frequency)
    return tuple(dumped[name] for name in CATALOG_COLUMNS)


def clean_series(data: SeriesResult) -> CleanSeriesData:
    """Group series rows by the fields they share, keeping every other field on each row."""
    groups = _grouped(data.rows, lambda row: tuple(getattr(row, name) for name in SERIES_GROUP))
    return CleanSeriesData(
        view="clean",
        series=tuple(_series(rows) for rows in groups),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=None if data.as_of is None else data.as_of.isoformat(),
        all_versions=data.all_versions,
        availability_note=data.availability_note,
        availability=data.availability,
        explanation=None if data.rows else NO_ROWS,
    )


def _series(rows: list[SeriesRow]) -> CleanSeries:
    first = rows[0]
    return CleanSeries(
        series_key=first.series_key,
        dataset=first.dataset,
        kind=first.kind,
        provider=first.provider,
        unit=first.unit,
        indicator_id=first.indicator_id,
        columns=SERIES_COLUMNS,
        rows=_table(rows, SERIES_COLUMNS),
    )


def clean_derived(data: DerivedResult) -> CleanDerivedData:
    """Group derived rows by definition and subject, keeping every other field on each row."""
    groups = _grouped(data.rows, lambda row: tuple(getattr(row, name) for name in DERIVED_GROUP))
    return CleanDerivedData(
        view="clean",
        groups=tuple(_derived_group(rows) for rows in groups),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=None if data.as_of is None else data.as_of.isoformat(),
        availability_note=data.availability_note,
        availability=data.availability,
        explanation=None if data.rows else NO_ROWS,
    )


def _derived_group(rows: list[DerivedRow]) -> CleanDerivedGroup:
    first = rows[0]
    value_index = DERIVED_COLUMNS.index("value")
    table = tuple(
        (*row[:value_index], exact(source.value), *row[value_index + 1 :])
        for row, source in zip(_table(rows, DERIVED_COLUMNS), rows, strict=True)
    )
    return CleanDerivedGroup(
        indicator_id=first.indicator_id,
        indicator_version=first.indicator_version,
        subject=first.subject,
        unit=first.unit,
        formula=first.formula,
        columns=DERIVED_COLUMNS,
        rows=table,
    )


def clean_panel(data: PanelResult) -> CleanPanelData:
    """Turn the panel into a value table and list every present cell's details."""
    return CleanPanelData(
        view="clean",
        columns=("observed_on", *data.columns),
        rows=tuple(_panel_row(data, index) for index in range(len(data.rows))),
        cell_columns=CELL_COLUMNS,
        cells=tuple(_panel_cells(data)),
        column_details=tuple(_panel_column(data, name) for name in data.columns),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=None if data.as_of is None else data.as_of.isoformat(),
        calendar=data.calendar,
        fill=data.fill,
        availability_note=data.availability_note,
        availability=data.availability,
        explanation=None if data.rows else NO_ROWS,
    )


def _panel_row(data: PanelResult, index: int) -> Row:
    row = data.rows[index]
    values: list[JsonValue] = [row.observed_on.isoformat()]
    for name in data.columns:
        cell = row.cells.get(name)
        values.append(None if cell is None else cell.value)
    return tuple(values)


def _panel_cells(data: PanelResult) -> Iterable[Row]:
    for row in data.rows:
        for name in data.columns:
            cell = row.cells.get(name)
            if cell is None:
                continue
            dumped = cell.model_dump(mode="json")
            yield (
                row.observed_on.isoformat(),
                name,
                dumped["value"],
                dumped["observed_on"],
                dumped["known_since"],
                dumped["filled"],
                dumped["stale_days"],
                dumped["availability"],
            )


def _panel_column(data: PanelResult, name: str) -> CleanPanelColumn:
    cells = [row.cells.get(name) for row in data.rows]
    present = [cell for cell in cells if cell is not None]
    return CleanPanelColumn(
        name=name,
        absent_cells=len(cells) - len(present),
        null_values=sum(cell.value is None for cell in present),
        filled_cells=sum(cell.filled for cell in present),
        stale_cells=sum(cell.stale_days > 0 for cell in present),
    )


def clean_conditional_returns(data: ConditionalReturnsResult) -> CleanConditionalReturnsData:
    """Put each horizon's outcome beside its baseline and write each event as one row."""
    return CleanConditionalReturnsData(
        view="clean",
        target_symbol=data.target_symbol,
        trigger=data.trigger,
        entry=data.entry,
        as_of=None if data.as_of is None else data.as_of.isoformat(),
        evaluated_days=data.evaluated_days,
        undetermined_days=data.undetermined_days,
        state_days=data.state_days,
        event_count=data.event_count,
        horizons=tuple(_horizon(data, summary) for summary in data.summary),
        event_columns=EVENT_COLUMNS,
        condition_columns=CONDITION_COLUMNS,
        events=tuple(_event_row(event) for event in data.events),
        truncated=data.truncated,
        notes=data.notes,
        availability_note=data.availability_note,
        availability=data.availability,
        explanation=study_explanation(data),
    )


def study_explanation(data: ConditionalReturnsResult) -> str | None:
    """Say why a study has nothing to measure.

    The Data Interface counts undetermined days within the evaluated window, so "mostly
    undetermined" compares them with half of ``evaluated_days``.
    """
    if data.evaluated_days == 0:
        return NO_EVALUATED_DAYS
    if data.undetermined_days * 2 > data.evaluated_days:
        return MOSTLY_UNDETERMINED
    return NO_EVENTS if data.event_count == 0 else None


def _horizon(data: ConditionalReturnsResult, summary: HorizonSummary) -> CleanHorizon:
    baseline = next((item for item in data.baseline if item.horizon == summary.horizon), None)
    return CleanHorizon(
        horizon=summary.horizon,
        observations=summary.observations,
        mean=summary.mean,
        median=summary.median,
        hit_rate=summary.hit_rate,
        minimum=summary.minimum,
        maximum=summary.maximum,
        baseline_observations=None if baseline is None else baseline.observations,
        baseline_mean=None if baseline is None else baseline.mean,
        baseline_median=None if baseline is None else baseline.median,
        baseline_hit_rate=None if baseline is None else baseline.hit_rate,
        baseline_minimum=None if baseline is None else baseline.minimum,
        baseline_maximum=None if baseline is None else baseline.maximum,
    )


def _event_row(event: ConditionalEvent) -> Row:
    dumped = event.model_dump(mode="json")
    dumped["conditions"] = [list(row) for row in _table(event.conditions, CONDITION_COLUMNS)]
    return tuple(dumped[name] for name in EVENT_COLUMNS)
