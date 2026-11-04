"""Reshape raw Data Interface results into the clean view.

These functions only regroup, rename and format. They never compute a new number: every value in
a clean result is a value from the raw result, which the golden tests check.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

from stram_data.data_interface import (
    AvailabilitySummary,
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

from stram_quant_mcp.clean_models import (
    CleanAvailability,
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
from stram_quant_mcp.tool_models import CatalogData

# Spellings of one frequency seen across providers, mapped to one word each.
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
    "series_key",
    "name",
    "dataset",
    "kind",
    "provider",
    "unit",
    "frequency",
    "first_observation",
    "last_observation",
    "record_count",
)
SERIES_COLUMNS = ("observed_on", "value", "known_since", "flags")
DERIVED_COLUMNS = ("observation_date", "value", "known_since", "event_id", "flags")
PANEL_DETAIL_COLUMNS = ("observed_on", "column", "value_observed_on", "stale_days", "filled")
EVENT_FIXED_COLUMNS = ("event_date", "entry_date", "entry_close")
NO_ROWS = (
    "No rows matched in this window and availability mode. A value counts only from its "
    "known_since date, so history loaded recently may not be usable for earlier dates in strict "
    "mode; try availability='estimated', a later window or different filters."
)
MOSTLY_UNDETERMINED = (
    "Most days in the window were undetermined: the condition's data was not yet known on those "
    "days in this availability mode, so it could not be evaluated. Try availability='estimated' "
    "or a later start_date before reading the statistics."
)


def iso(value: date | None) -> str | None:
    """Format a date as ISO, keeping a missing date missing."""
    return None if value is None else value.isoformat()


def exact(value: Decimal | None) -> str | None:
    """Format an exact decimal without trailing zeros; its value does not change."""
    if value is None:
        return None
    text = f"{value.normalize():f}"
    return "0" if text in {"-0", "0"} else text


def frequency(value: str | None) -> str | None:
    """Return the one word for a frequency spelling, or the spelling itself if unknown."""
    if value is None:
        return None
    return FREQUENCIES.get(value.strip().lower(), value)


def availability(summary: AvailabilitySummary) -> CleanAvailability:
    """Keep the availability facts an agent acts on; policy texts stay in the raw view."""
    return CleanAvailability(
        mode=summary.mode,
        hindsight_only=summary.hindsight_only,
        unavailable_rows=summary.unavailable_rows,
        uncovered=summary.uncovered,
        warnings=summary.warnings,
    )


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
        limit_reached=len(entries) >= limit,
        by_dataset=dict(Counter(entry.dataset for entry in entries)),
        by_provider=dict(Counter(entry.provider for entry in entries)),
    )


def _catalog_row(entry: CatalogEntry) -> Row:
    return (
        entry.series_key,
        entry.name,
        entry.dataset,
        entry.kind,
        entry.provider,
        entry.unit,
        frequency(entry.frequency),
        iso(entry.first_observation),
        iso(entry.last_observation),
        entry.record_count,
    )


def clean_series(data: SeriesResult) -> CleanSeriesData:
    """Group series rows by series, keeping each value with its known_since date."""
    groups = _grouped(
        data.rows,
        lambda row: (
            row.dataset,
            row.series_key,
            row.indicator_id,
            row.kind,
            row.provider,
            row.unit,
        ),
    )
    return CleanSeriesData(
        view="clean",
        series=tuple(_series(rows) for rows in groups),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=iso(data.as_of),
        all_versions=data.all_versions,
        availability_note=data.availability_note,
        availability=availability(data.availability),
        explanation=NO_ROWS if not data.rows else None,
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
        rows=tuple(
            (iso(row.observed_on), row.value, iso(row.known_since), _series_flags(row))
            for row in rows
        ),
    )


def _series_flags(row: SeriesRow) -> str | None:
    flags = [*row.quality_flags, *(["vintage"] if row.is_vintage else [])]
    if row.availability is not None:
        flags.append(f"availability:{row.availability.kind}")
    return ",".join(flags) or None


def clean_derived(data: DerivedResult) -> CleanDerivedData:
    """Group derived rows by indicator and subject, with the definition stated once."""
    groups = _grouped(
        data.rows,
        lambda row: (row.indicator_id, row.subject, row.indicator_version, row.unit, row.formula),
    )
    return CleanDerivedData(
        view="clean",
        groups=tuple(_derived_group(rows) for rows in groups),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=iso(data.as_of),
        availability_note=data.availability_note,
        availability=availability(data.availability),
        explanation=NO_ROWS if not data.rows else None,
    )


def _derived_group(rows: list[DerivedRow]) -> CleanDerivedGroup:
    first = rows[0]
    bases = {row.availability_basis for row in rows}
    shared_basis = len(bases) == 1
    return CleanDerivedGroup(
        indicator_id=first.indicator_id,
        indicator_version=first.indicator_version,
        subject=first.subject,
        unit=first.unit,
        formula=first.formula,
        availability_basis=first.availability_basis if shared_basis else None,
        columns=DERIVED_COLUMNS,
        rows=tuple(
            (
                iso(row.observation_date),
                exact(row.value),
                iso(row.known_since),
                row.event_id,
                _derived_flags(row, show_basis=not shared_basis),
            )
            for row in rows
        ),
    )


def _derived_flags(row: DerivedRow, *, show_basis: bool) -> str | None:
    """List what is unusual about a derived row; the basis only when rows disagree on it."""
    flags = [
        *(["retracted"] if row.retracted else []),
        *(["licensed_input"] if row.licensed_input else []),
        *([f"basis:{row.availability_basis}"] if show_basis else []),
    ]
    if row.availability is not None:
        flags.append(f"availability:{row.availability.kind}")
    return ",".join(flags) or None


def clean_panel(data: PanelResult) -> CleanPanelData:
    """Turn the panel into a value table, with cell details summarized per column."""
    return CleanPanelData(
        view="clean",
        columns=("observed_on", *data.columns),
        rows=tuple(
            (iso(row.observed_on), *(_cell_value(row.cells.get(name)) for name in data.columns))
            for row in data.rows
        ),
        column_details=tuple(_panel_column(data, name) for name in data.columns),
        filled_or_stale=tuple(_panel_details(data)),
        count=data.count,
        limit=data.limit,
        truncated=data.truncated,
        as_of=iso(data.as_of),
        calendar=data.calendar,
        fill=data.fill,
        availability_note=data.availability_note,
        availability=availability(data.availability),
        explanation=NO_ROWS if not data.rows else None,
    )


def _cell_value(cell: object) -> float | None:
    return getattr(cell, "value", None)


def _panel_column(data: PanelResult, name: str) -> CleanPanelColumn:
    cells = [row.cells.get(name) for row in data.rows]
    present = [cell for cell in cells if cell is not None]
    known = sorted(cell.known_since for cell in present)
    return CleanPanelColumn(
        name=name,
        missing_cells=sum(cell is None or cell.value is None for cell in cells),
        filled_cells=sum(cell.filled for cell in present),
        stale_cells=sum(cell.stale_days > 0 for cell in present),
        known_since_first=iso(known[0]) if known else None,
        known_since_last=iso(known[-1]) if known else None,
    )


def _panel_details(data: PanelResult) -> Iterable[Row]:
    for row in data.rows:
        for name in data.columns:
            cell = row.cells.get(name)
            if cell is not None and (cell.filled or cell.stale_days > 0):
                yield (
                    iso(row.observed_on),
                    name,
                    iso(cell.observed_on),
                    cell.stale_days,
                    cell.filled,
                )


def clean_conditional_returns(data: ConditionalReturnsResult) -> CleanConditionalReturnsData:
    """Put each horizon's outcome beside its baseline and shorten events to one row each."""
    mostly_undetermined = data.undetermined_days * 2 > data.evaluated_days
    return CleanConditionalReturnsData(
        view="clean",
        target_symbol=data.target_symbol,
        trigger=data.trigger,
        entry=data.entry,
        as_of=iso(data.as_of),
        evaluated_days=data.evaluated_days,
        undetermined_days=data.undetermined_days,
        state_days=data.state_days,
        event_count=data.event_count,
        horizons=tuple(_horizon(data, summary) for summary in data.summary),
        event_columns=(*EVENT_FIXED_COLUMNS, *(f"return_{h}" for h in data.horizons)),
        events=tuple(_event_row(event, data.horizons) for event in data.events),
        truncated=data.truncated,
        notes=data.notes,
        availability_note=data.availability_note,
        availability=availability(data.availability),
        explanation=MOSTLY_UNDETERMINED if mostly_undetermined else None,
    )


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
        baseline_observations=baseline.observations if baseline else None,
        baseline_mean=baseline.mean if baseline else None,
        baseline_median=baseline.median if baseline else None,
        baseline_hit_rate=baseline.hit_rate if baseline else None,
    )


def _event_row(event: ConditionalEvent, horizons: tuple[int, ...]) -> Row:
    returns = tuple(event.forward_returns.get(horizon) for horizon in horizons)
    return (iso(event.event_date), iso(event.entry_date), event.entry_close, *returns)


def _grouped[RowT](rows: Iterable[RowT], key: Callable[[RowT], object]) -> list[list[RowT]]:
    groups: dict[object, list[RowT]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return list(groups.values())
