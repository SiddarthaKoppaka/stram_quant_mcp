"""Caller-facing tool descriptions, rendered from one table so they cannot drift apart.

Each description tells a model or developer what the tool is for, what it is not for, the exact
input bounds, what comes back, and every failure it can return with the retry guidance for each.
"""

from __future__ import annotations

from dataclasses import dataclass

COMMON_ERRORS = (
    "Failures return isError=true and _meta['io.stram/error'] with code, category, retryable, "
    "reason and next_action:\n"
    "- invalid_request: an argument is wrong; the reason names the field. Fix it. Not retryable.\n"
    "- access_denied: the token lacks this tool's scope. Not retryable; ask the administrator.\n"
    "- unavailable: the Data Plane is down or paused. Retryable with backoff; honour "
    "retry_after_seconds.\n"
    "- overloaded: capacity or rate limit reached. Retryable after retry_after_seconds.\n"
    "- deadline_exceeded: too slow. Retry once with a narrower range or fewer series.\n"
    "- data_plane_error, service_credentials_rejected, data_plane_forbidden: a server-side fault. "
    "Not retryable; report the request_id."
)
EXTRA_ERRORS = {
    "not_found": "- not_found: no such entity. Check the id with a discovery tool. Not retryable.",
    "resource_limit": (
        "- resource_limit: the result is too large. Reduce limit, window or series. Not retryable "
        "unchanged."
    ),
}


@dataclass(frozen=True, slots=True)
class ToolDocs:
    """The facts a description is rendered from."""

    summary: str
    use_when: str
    avoid: str
    inputs: str
    returns: str
    extra_errors: tuple[str, ...] = ()

    def render(self) -> str:
        """Return the complete description text."""
        extras = "".join(f"\n{EXTRA_ERRORS[name]}" for name in self.extra_errors)
        return (
            f"{self.summary}\n\n"
            f"Use when: {self.use_when}\n"
            f"Do not use for: {self.avoid}\n"
            f"Inputs: {self.inputs}\n"
            f"Returns: {self.returns}\n"
            "Read-only and idempotent; safe to call again.\n\n"
            f"{COMMON_ERRORS}{extras}"
        )


TOOL_DOCS: dict[str, ToolDocs] = {
    "quant_list_series": ToolDocs(
        "List the quantitative series available in the read-only catalog.",
        "you need to discover which series keys, providers, units and coverage exist before "
        "querying data.",
        "reading observations (use quant_get_series).",
        "optional dataset (economic_observation, market_bar, positioning_report); limit 1-500, "
        "default 100; view raw|clean.",
        "catalog entries with dataset, provider, series key, unit, frequency, counts and "
        "coverage; limit_filled in the clean view means more entries may exist.",
    ),
    "quant_list_indicators": ToolDocs(
        "List the derived indicators and their coverage.",
        "you need a valid indicator_id for quant_get_derived or quant_list_indicator_subjects.",
        "reading indicator values (use quant_get_derived).",
        "none.",
        "indicators with id, description, formula, version and coverage.",
    ),
    "quant_list_indicator_subjects": ToolDocs(
        "List the subjects (for example symbols) available for one derived indicator.",
        "choosing the subject filter for quant_get_derived after quant_list_indicators.",
        "indicators without subjects; they return an empty list.",
        "indicator_id (1-100 characters).",
        "a list of subject keys.",
        ("not_found",),
    ),
    "quant_describe_availability": ToolDocs(
        "Describe the versioned historical-availability policies in force.",
        "you must understand what strict, estimated and ex_post mean for each dataset before "
        "interpreting results.",
        "reading data.",
        "none.",
        "each policy's dataset, version, evidence rules and known limitations.",
    ),
    "quant_get_freshness": ToolDocs(
        "Report how fresh each quantitative dataset is, with known limitations.",
        "you must check that data is current enough before relying on it.",
        "history; it reports recency only.",
        "none.",
        "per-dataset latest observation, ingestion time and caveats.",
    ),
    "quant_get_series": ToolDocs(
        "Read bounded quantitative series with revisions and availability semantics preserved.",
        "you need observations for known series_keys, indicator_ids or a provider over a date "
        "range.",
        "listing what exists (use quant_list_series) or computed indicators (use "
        "quant_get_derived).",
        "start_date and end_date (inclusive, at most 1830 days apart); at least one of "
        "series_keys, indicator_ids, provider; kinds; as_of; all_versions; limit 1-500 (default "
        "100); availability strict (default) | estimated | ex_post (hindsight-only, cannot be "
        "combined with as_of); view raw|clean.",
        "rows with value, unit, quality flags, known_since, revision and lineage ids; "
        "truncated=true means narrow the range or filters.",
        ("resource_limit",),
    ),
    "quant_get_derived": ToolDocs(
        "Read one bounded derived indicator with exact decimal values and lineage identifiers.",
        "you need computed indicators such as yield spreads over a date range.",
        "raw source observations (use quant_get_series).",
        "indicator_id; optional subject; start_date and end_date (at most 1830 days); as_of; "
        "limit 1-500; availability strict | estimated | ex_post; view raw|clean.",
        "rows with exact decimal strings, input event ids, availability basis and retraction "
        "state.",
        ("resource_limit",),
    ),
    "quant_get_lineage": ToolDocs(
        "Resolve the source observations behind one derived event identifier.",
        "you must audit where a derived value came from, using an event_id from quant_get_derived.",
        "searching; it needs an exact event id.",
        "event_id (UUID).",
        "the derived event with its input observations and batches.",
        ("not_found",),
    ),
    "quant_get_aligned_panel": ToolDocs(
        "Align up to 8 series on an explicit calendar with bounded gap handling.",
        "you need to compare series side by side on one calendar (union, intersection or "
        "first_column), optionally with forward fill.",
        "single-series reads (use quant_get_series).",
        "columns (1-8: name, series_key, optional indicator_id); start_date and end_date (at "
        "most 1830 days); calendar; fill none|forward; max_staleness_days 0-366; limit 1-500; "
        "availability; view raw|clean.",
        "a date-by-column table where every cell keeps its observation date, known_since, "
        "filled flag and stale_days.",
        ("resource_limit",),
    ),
    "quant_get_conditional_returns": ToolDocs(
        "Compute bounded forward returns after conditions became known, with a baseline.",
        "you need to study how a target symbol behaved after signal conditions on other series "
        "became known.",
        "forecasting; this is a historical study, not a prediction.",
        "target_symbol; conditions (operator gt|ge|lt|le|rising|falling with threshold); "
        "horizons; start_date and end_date (at most 1830 days); availability; view raw|clean.",
        "per-horizon outcome statistics beside a baseline, plus the contributing events with "
        "their lineage.",
        ("resource_limit",),
    ),
    "quant_get_latest_quotes": ToolDocs(
        "Read the latest stored Alpaca IEX top-of-book quotes for a bounded symbol set.",
        "a screen or analysis needs fresh IEX bid and ask data with its exact event time and "
        "availability state.",
        "daily closes, trades, consolidated market data, or a last price; this tool returns "
        "only IEX quotes and never substitutes another value.",
        "symbols (1-30 distinct uppercase symbols using A-Z, digits, '.', '/' or '-'); max_age_ms "
        "1-15000, default 15000.",
        "one result per requested symbol in request order. Each result has status, reason, "
        "provenance and, only when fresh, bid and ask values.",
    ),
}
