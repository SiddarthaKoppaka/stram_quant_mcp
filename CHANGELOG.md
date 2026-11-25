# Changelog

## Unreleased — quant_get_series: newest first, and up to 5,000 rows

- `quant_get_series` gains an optional `order`, `asc` (the default, oldest first) or `desc` (newest
  first), passed to the Data Interface `SeriesQuery.order`, and its `limit` maximum rises from 500 to
  5,000 (the Data Interface `MAX_LIMIT`); the default limit (100) and the default order are unchanged.
  With `desc` a cut keeps the newest rows, so a year or five years of daily closes is one call. History
  beyond the 1,830-day range is read by moving `end_date` to the day before the oldest returned
  `observed_on` while `truncated` is true; there is no cursor. The tool text says so. Only the schema
  hash of `quant_get_series` changes (the other twelve tools are byte-identical); the schema artifact is
  regenerated, and STRAM's pinned copy must be updated with this release.
- A default call sends the Data Plane the request it sent before: `order` is left out of the body when it
  is `asc`, so it still works against a Data Plane older than 1.15. `order=desc` needs Data Plane 1.15 or
  later; the minimum service version for startup is unchanged (1.11).
- Moves the `stram-data` pin to 1.16.0 at the merged commit `f4396f5d8e4c65016a71c6db8c39bb8e72acb80a`
  (CI ref, dependency script, lock, image wheel, release snapshot) and the image's Debian snapshot to
  2026-11-24, as the Qual MCP did.

## Unreleased — identity providers that need the audience named

- The Data Plane client can name the audience and scopes on every token request: the optional
  `STRAM_MCP_DATA_RESOURCE` (the Data Plane audience, sent as `resource`) and `STRAM_MCP_DATA_SCOPES`
  (space-separated base scopes, joined with a route's own extra scope). Logto issues a token only for a
  named audience and scopes; Keycloak maps both from the client, which is why this was never needed.
  Left unset, the request is byte-identical to before, so nothing changes until an operator sets them.

## Unreleased — fundamental ratios

- Adds `quant_get_fundamental_ratios` (scope `quant:read`): growth, profitability, cash-flow,
  liquidity, leverage and valuation ratios, each with its formula, the inputs it used and a note,
  through the Data Plane's `POST /v1/fundamentals/ratios`. Thirteen tools now; the existing twelve
  are unchanged (their schemas and hashes are byte-identical).
- Moves the `stram-data` pin to 1.12.0 at `2eaf9aef309e3be3ed6d575cbc84d7813b1ea6be` and requires
  Data Plane service 1.11 or later; the Data Plane must be upgraded before this release starts.

## Unreleased — company fundamentals

- Adds `quant_get_fundamentals` (scope `quant:read`): reported company financials from SEC XBRL
  filings through the Data Plane's `POST /v1/fundamentals`, as filed by an optional `as_of`. Twelve
  tools now; the existing eleven are unchanged.
- Pins `stram-data` 1.10.0 at `13cd0913fafcc31115bd7cab8b91fa3858d902d0` and requires Data Plane
  service 1.10 or later; the Data Plane must be upgraded before this release starts.

## Unreleased — clean view

- Adds an optional `view` input, `raw` (the default) or `clean`, to `quant_list_series`,
  `quant_get_series`, `quant_get_derived`, `quant_get_aligned_panel` and
  `quant_get_conditional_returns`. The raw result is unchanged. The clean result is lossless: fields
  a group of rows shares are stated once, every other field stays on its row, and rows become tuples
  named by `columns`. Exact decimals drop trailing zeros as text, frequencies gain a normalized
  spelling beside the original, and empty or undetermined results gain a plain explanation. Golden
  tests rebuild every raw row from the clean view.
- The five tools' input and output schemas, and so their schema hashes, change. Clients that pin
  hashes must update them.

## Previous — FastMCP server

- Replaces the hand-wired SDK server with a FastMCP 4.1.6 server. The ten tool names, closed
  schemas, scopes and schema hashes are unchanged; a black-box behavior suite pins them.
- Every tool call is re-authorized against a default-deny policy store, tools a caller cannot use are
  hidden from `tools/list`, and a caller who keeps being denied is blocked.
- Denied calls now reply `Access denied.` and error replies carry a `retryable` flag in `_meta`.
  The previous message, `The caller is not authorized to invoke this tool.`, is retired.
- Verifies tokens with a strict FastMCP verifier behind the Keycloak provider, and refuses to become
  ready until the JWKS endpoint serves a key.
- Documentation is reorganised into tutorials, how-to guides, reference and explanation.
- Removes the hand-wired SDK server and its `STRAM_MCP_SERVER_IMPLEMENTATION` switch. The old code is kept
  on the `archive/sdk-server` branch.

## 0.1.0 release candidate — 2026-11-01

- Implements ten authenticated, authorized, bounded and audited read-only MCP tools backed only by
  pinned `stram-data` 1.3.1 Data Interface v1.
- Adds durable HMAC-chained terminal audit outcomes, bounded disposable query workers, failure-closed
  readiness and graceful shutdown.
- Adds Linux kernel seccomp isolation for the worker's socket and `io_uring` paths, plus a native
  container verification command.
- Adds real MCP SDK HTTP integration against a synthetic lake, contract/security/reliability gates,
  reproducible container/SBOM checks and Diátaxis-oriented engineering/operator documentation.
- Documents the reviewed PyArrow 22.0.0 security exception and expiry/removal trigger.

This is a staging qualification candidate, not a production release. SIDDU identity, read-only mount,
private TLS and egress policy, test IdP, audit custody, restore and rollback remain staging gates.
