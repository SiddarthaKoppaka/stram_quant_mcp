# Changelog

## Unreleased — clean view

- Adds an optional `view` input, `raw` (the default) or `clean`, to `quant_list_series`,
  `quant_get_series`, `quant_get_derived`, `quant_get_aligned_panel` and
  `quant_get_conditional_returns`. The raw result is unchanged. The clean result holds the same
  values in a shape sized for agents: shared fields once per series, rows as short tuples, exact
  decimals without trailing zeros, one spelling per frequency, catalog counts by dataset and provider,
  and a plain explanation when a result is empty or mostly undetermined. No value is computed;
  golden tests compare every clean value with its raw value.
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
