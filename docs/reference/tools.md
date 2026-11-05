# Tool reference

This is the operator and consumer reference for the ten tools implemented in
`src/stram_quant_mcp/tool_registry.py`. Machine-readable schemas and reviewed SHA-256 hashes are in
`contracts/tool_schemas.json`; MCP `tools/list` returns the same input/output schemas and read-only
annotations.

## Common contract

All inputs are closed: unknown properties are rejected. Dates are ISO `YYYY-MM-DD`; date ranges are
inclusive and limited to 1,830 days. Keys are 1 to 100 characters from
`A-Za-z0-9_.:-^=/` plus space. Every tool is read-only, idempotent and closed-world.

Successful calls return structured content with this envelope:

```json
{
  "tool_version": "0.1.0",
  "stram_data_version": "1.3.1",
  "interface_version": "1",
  "policy_version": "availability-policy-hash",
  "request_id": "server-generated-id",
  "data": {},
  "warnings": []
}
```

`data` is the exact typed Data Interface result or a thin collection wrapper. Derived `Decimal`
values are JSON strings so precision is preserved. Null, zero, false, missing and unavailable remain
distinct. Results retain units, quality flags, record/event IDs, revision visibility, lineage,
retractions, truncation, availability evidence and limitations.

Every call needs a valid JWT and the listed exact scope. Authorization is enforced before worker
dispatch. Invalid inputs and unknown entities are not retried. Overload, deadline and unavailable
errors are retryable only after the caller applies its own bounded policy. MCP errors contain a safe
message and `isError=true`; they do not expose paths, SQL, dependency details or stack traces.

## Clean view

`quant_list_series`, `quant_get_series`, `quant_get_derived`, `quant_get_aligned_panel` and
`quant_get_conditional_returns` accept an optional `view`: `raw` (the default) or `clean`. Other tools
reject it. Both views come from the same Data Interface call and carry the same envelope and
warnings; the raw result is exactly as described below.

The clean view is **lossless**: every raw row can be rebuilt from it exactly. Fields that every row of
a group shares are stated once in the group; every other field stays on its row, including IDs,
timestamps, lineage IDs and availability evidence. Rows are tuples named by `columns`. The result is
marked `"view": "clean"` and carries the raw `availability` summary unchanged.

| Tool | Clean shape |
|---|---|
| `quant_list_series` | One table of every catalog field plus `frequency_normalized`, counts `by_dataset` and `by_provider` of the returned entries, and `limit_filled` when the result filled the limit, which means more entries may exist |
| `quant_get_series` | `series` grouped by dataset, kind, provider, series key, indicator and unit; each row keeps `observed_on`, `observed_at`, `value`, `known_since`, `is_vintage`, `ingested_at`, `event_id`, `record_id`, `quality_flags` and `availability` |
| `quant_get_derived` | `groups` by indicator, version, subject, unit and formula; each row keeps `observation_date`, `value`, `known_since`, `computed_at`, `event_id`, `input_event_ids`, `input_batch_ids`, `availability_basis`, `licensed_input`, `retracted` and `availability` |
| `quant_get_aligned_panel` | A value table (`observed_on` plus one column per series) and `cells`, which lists every present cell with its value, observation date, `known_since`, `filled`, `stale_days` and `availability`. A cell absent from `cells` was absent in the raw panel; a listed cell with a null value was present with no value |
| `quant_get_conditional_returns` | `horizons` with each outcome's six statistics beside the baseline's, and `events` as rows of `event_columns`, whose `conditions` are rows of `condition_columns` |

Only two things change form. Exact decimals drop trailing zeros as text, never by rounding
(`0.460000000000` becomes `0.46`). Frequency spellings such as `D`, `day` and `daily` are also given
as one word in `frequency_normalized`, beside the original `frequency`. When no rows match, or a
study has no evaluated days, no events, or mostly undetermined days, `explanation` says why in plain
words.

```json
{"series_keys":["SPY"],"kinds":["market_bar"],"start_date":"2026-10-01","end_date":"2026-10-31","view":"clean"}
```

## Discovery tools

### `quant_list_series`

- Scope: `quant:discover`
- Data Interface: `get_catalog(dataset=..., limit=...)`
- Input: optional `dataset` (`economic_observation`, `market_bar`, `positioning_report`); `limit` 1 to
  500, default 100.
- Data: `entries`, each carrying dataset/provider/series metadata, units, frequency, counts and
  coverage as supplied by the Data Interface.

```json
{"dataset":"market_bar","limit":20}
```

### `quant_list_indicators`

- Scope: `quant:discover`
- Data Interface: `list_indicators()`
- Input: `{}` only.
- Data: `indicators`, including indicator ID, version, unit, subject count and coverage.

### `quant_list_indicator_subjects`

- Scope: `quant:discover`
- Data Interface: `list_indicator_subjects(indicator_id)`
- Input: required `indicator_id` key.
- Data: `subjects`, a tuple of available subjects. Unknown indicators return not-found.

```json
{"indicator_id":"equity_return_1d"}
```

### `quant_describe_availability`

- Scope: `quant:discover`
- Data Interface: `describe_availability_policies()`
- Input: `{}` only.
- Data: `policies` with policy ID, version, applicability, evidence, basis, parameters, sources,
  enabled state and limitations.

### `quant_get_freshness`

- Scope: `quant:discover`
- Data Interface: `get_freshness()`
- Input: `{}` only.
- Data: the Data Interface freshness report, including dataset/provider recency and caveats. Freshness
  is descriptive and is not proof of completeness.

## Read tools

### `quant_get_series`

- Scope: `quant:read`
- Data Interface: `get_series(SeriesQuery)`
- Required: `start_date`, `end_date`, and at least one of `series_keys`, `indicator_ids` or `provider`.
- Filters: up to 10 `series_keys`; up to 10 `indicator_ids`; optional `provider`; non-empty `kinds`
  drawn from `economic`, `rates`, `market_bar`, `positioning`, `derived` (default all).
- Controls: optional `as_of`; `all_versions` default false; `limit` 1 to 500, default 100;
  `availability` default `strict`.
- Data: rows plus count, limit, `truncated`, `as_of`, availability note and availability summary.

```json
{
  "series_keys":["SPY"],
  "kinds":["market_bar"],
  "start_date":"2026-10-01",
  "end_date":"2026-10-02",
  "availability":"strict",
  "limit":100
}
```

For revision history, set `all_versions=true`. With `as_of`, only versions available by that date in
the selected mode are returned. There is no cursor; if `truncated=true`, narrow the request.

### `quant_get_derived`

- Scope: `quant:read`
- Data Interface: `get_derived(DerivedQuery)`
- Required: `indicator_id`, `start_date`, `end_date`.
- Optional: `subject`, `as_of`, `availability`; `limit` 1 to 500, default 100.
- Data: derived rows with exact decimal value or null, formula, indicator version, input event/batch
  IDs, retraction state, availability and truncation.

```json
{
  "indicator_id":"treasury_10y_minus_2y",
  "start_date":"2026-10-01",
  "end_date":"2026-10-31"
}
```

### `quant_get_lineage`

- Scope: `quant:lineage`
- Data Interface: `get_lineage(event_id)`
- Input: one UUID `event_id`.
- Data: the derived event and its source observations, plus unresolved input IDs. Unknown IDs return
  not-found.

## Analytics tools

### `quant_get_aligned_panel`

- Scope: `quant:analytics`
- Data Interface: `get_aligned_panel(PanelQuery)`
- Required: `start_date`, `end_date`, and 1 to 8 `columns`. Each column has a unique 1 to 40 character
  alphanumeric/underscore `name`, a `series_key`, and optional `indicator_id`.
- Optional: `as_of`; `calendar` is `union` (default), `intersection` or `first_column`; `fill` is
  `none` (default) or `forward`; `max_staleness_days` is 0 to 366; `limit` 1 to 500, default 100;
  `availability` default `strict`.
- Constraint: forward fill requires positive `max_staleness_days`.
- Data: named columns and aligned rows. Unknown cells remain null; the service does not invent values.

```json
{
  "columns":[
    {"name":"spy","series_key":"SPY"},
    {"name":"ten_year","series_key":"UST_10YEAR"}
  ],
  "start_date":"2026-10-01",
  "end_date":"2026-10-31",
  "calendar":"union",
  "fill":"none"
}
```

### `quant_get_conditional_returns`

- Scope: `quant:analytics`
- Data Interface: `get_conditional_returns(ConditionalReturnsQuery)`
- Required: `target_symbol`, `start_date`, `end_date`, and 1 to 3 `conditions`.
- Condition: `series_key`, optional `indicator_id`, and operator `gt`, `ge`, `lt`, `le`, `rising` or
  `falling`. Comparison operators require a finite numeric `threshold`; direction operators prohibit
  one.
- Optional: 1 to 5 distinct `horizons` of 1 to 252 trading days (default 1, 5, 20); `trigger` is
  `onset` (default) or `while`; `entry` is `next_close` (default) or `same_close`; `as_of`;
  `availability`; `limit` 1 to 200, default 100.
- Data: condition events, signal availability/cutoff/entry times, source event IDs, forward returns,
  event count, unconditional baseline, availability and truncation.

```json
{
  "target_symbol":"SPY",
  "conditions":[{"series_key":"UST_10YEAR","operator":"gt","threshold":4.0}],
  "start_date":"2026-01-01",
  "end_date":"2026-10-31",
  "horizons":[1,5,20],
  "entry":"next_close",
  "availability":"strict"
}
```

Signal conditions are evaluated from when data became known, not its observation date. Unknown data
is undetermined, not false. The default next-close entry prevents same-close look-ahead.

## Availability modes

| Mode | Meaning | Client obligation |
|---|---|---|
| `strict` | Stored source vintage date where available, otherwise ingestion date | Default for point-in-time-safe reads |
| `estimated` | Versioned policy estimate capped by actual ingestion | Surface evidence, warnings and uncovered periods; never call estimates verified |
| `ex_post` | Latest revision available from observation date | Hindsight only; never present as what was known at the time |

`ex_post` with `as_of` is rejected. Every ex-post result carries `hindsight_only=true` and a warning.

## Failure and audit behavior

| Condition | Client result | Terminal audit outcome |
|---|---|---|
| Missing/invalid bearer token | HTTP 401 | `authentication_denied` |
| Disallowed Host/Origin | HTTP 421/403 | `transport_denied` |
| Wrong tenant, missing scope or no grant | MCP error `Access denied.`, not retryable | `authorization_denied` |
| Caller blocked after repeated denials | MCP error `Access denied.`, not retryable | `denial_throttled` |
| Invalid tool/input | MCP safe invalid-request error | `invalid_tool` or `error` |
| Rate or queue capacity | Retryable MCP overload error | `rate_limited` or `error` |
| Worker timeout/crash/resource cap | Safe retryable/non-retryable MCP error | `error` |
| Client cancellation | Request cancellation | `cancelled` after bounded cleanup |
| Success | Structured result | `success`, row count and truncation only |
| Audit append failure | Safe unavailable response | No call result is released; readiness becomes false |

### Error replies

Every failed call returns `isError=true` and one fixed message from a closed set; no exception text,
validation detail, path or stack trace is ever included. The FastMCP server also tells the client
whether retrying can help, in the result's `_meta`:

```json
{
  "isError": true,
  "content": [{"type": "text", "text": "Access denied."}],
  "_meta": {"io.stram/error": {"code": "access_denied", "retryable": false}}
}
```

| `code` | Message | `retryable` |
|---|---|---|
| `access_denied` | `Access denied.` | no |
| `invalid_request` | The tool arguments are invalid or outside the allowed bounds. | no |
| `not_found` | The requested quantitative data entity was not found. | no |
| `resource_limit` | The quantitative result exceeds the allowed resource budget. | no |
| `unavailable` | Quantitative data is temporarily unavailable. | yes, after a client-side backoff |
| `deadline_exceeded` | The quantitative query exceeded its deadline. | yes, after a client-side backoff |
| `overloaded` | The quantitative service is at capacity. | yes, after a client-side backoff |

The service never retries a data query itself. A caller that is denied is not told why, so it cannot
learn which scope, tenant or issuer check failed.

### Tool visibility

`tools/list` returns only the tools the caller may call, decided by the same policy store that
authorizes each call. A call to a tool the caller cannot use is refused as `Access denied.`, exactly
like a tool that exists but is not granted. A name outside the reviewed catalogue returns the
invalid-request reply.

Audit stores normalized accountability fields and selected data scope, not raw request/result payloads
or bearer tokens. Logs/traces/metrics store only allowlisted outcome metadata.
