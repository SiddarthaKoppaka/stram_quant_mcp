# FastMCP Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Quant MCP server from hand-wired SDK code to FastMCP without changing any tool contract, security control or audit behavior.

**Architecture:** Declare each tool as a small FastMCP function that delegates to the existing `QuantToolAdapter` through the existing policy, audit and isolated-worker layers. Replace only the custom tool registry and JWT verifier, and only where a spike proves FastMCP meets every existing check.

**Tech Stack:** Python 3.12+, FastMCP 4.1.x (one exact pin), official `mcp` SDK 2.3.0, Pydantic, pytest, Ruff, mypy, import-linter.

## Global Constraints

- Tool names, closed input/output schemas, scopes and schema hashes in `contracts/tool_schemas.json` MUST NOT change.
- `stram_data.data_interface` v1.3.1 is the only data API; `stram_data` is never modified.
- Every tool stays read-only, typed, bounded, authenticated, authorized, audited and tested.
- New and changed code follows `ENGINEERING.md` section 3.1 (R1 to R16) and 3.4; new files are never added to the lint baseline.
- No deploy, credential access, commit or push without separate authorization.
- Documentation follows Diataxis (`ENGINEERING.md` section 11) and `tools/check_docs.py` passes.

---

## Task 0: Rules and documentation (done)

- [x] Restructure `docs/` into tutorials, how-to, reference and explanation with `docs/README.md` index.
- [x] Add `ENGINEERING.md` sections 3.1 to 3.4, documentation rules and CI gates.
- [x] Enable the readability rules in `pyproject.toml` with a legacy baseline; add `lint-imports` contracts and a CI step.

## Task 1: Compatibility spike (throwaway, isolated environment)

**Purpose:** Prove or disprove, with evidence, that FastMCP can meet each existing security behavior before any production code moves.

**Files:**
- Create: `fastmcp_spike.py` and `test_spike.py` in a scratch directory outside the repository, so lint, the lockfile and coverage are unaffected
- Modify: `PLANS.md` (record the result as milestone M7 evidence)

**Interfaces:**
- Consumes: a locally generated RSA key and an in-process JWKS (as `tests/integration/test_mcp_client.py` does).
- Produces: a pass/fail table for questions Q1 to Q10 below, which decides Tasks 2 to 5.

Questions the spike MUST answer, each with a test:

| # | Question | Today's behavior to match |
|---|---|---|
| Q1 | Does an unauthenticated request get HTTP 401 with `WWW-Authenticate: Bearer ... resource_metadata="<origin>/.well-known/oauth-protected-resource/mcp"`? | exact header |
| Q2 | Is RFC 9728 metadata served with `resource`, `authorization_servers` and `bearer_methods_supported: ["header"]`? | exact fields |
| Q3 | Is a token for a different audience rejected, using exact match? | 401 |
| Q4 | Is a token without a tool's scope denied, and does `tools/list` behavior stay acceptable? | per-tool exact scope |
| Q5 | Can tool code read verified claims (`tenant_id`, `sub`, `azp`) and enforce the tenant? | tenant from verified claims only |
| Q6 | Do the generated tool schemas keep `additionalProperties: false` and the same hash? | schema-hash gate |
| Q7 | Can Host and Origin allowlists (DNS-rebinding protection) be enforced exactly? | 421 / 403 |
| Q8 | Is stateless JSON-response HTTP mode available? | stateless, JSON |
| Q9 | Can middleware write exactly one terminal audit record per call, including denials? | one record |
| Q10 | Are issuer, expiry, RS256-only and `iat` checks enforced? | fail closed |

- [x] **Step 1: Create an isolated environment (does not touch `uv.lock`).**

```bash
uv venv /tmp/fastmcp-spike-env --python 3.13
uv pip install --python /tmp/fastmcp-spike-env/bin/python "fastmcp==4.1.6" "pyjwt[crypto]" pytest httpx
```

Expected: install succeeds. Record the resolved versions of `fastmcp` and `mcp`.

- [x] **Step 2: Write one failing test per question in `test_spike.py`, then implement `fastmcp_spike.py` until each passes or is proven impossible.** Record any question that cannot pass, with the exact observed behavior.

- [x] **Step 3: Run and capture the result table.**

Run: `/tmp/fastmcp-spike-env/bin/python -m pytest test_spike.py -v` from the scratch directory

Expected: a pass/fail line per question.

- [x] **Step 4: Decide.** If Q1 to Q4 and Q7 to Q10 pass, proceed to Task 2. If any fails, record which concern stays custom (for example keep the existing verifier) or stop the migration. Write the decision in `PLANS.md`.

### Spike result (2026-11-03): proceed, with five conditions

FastMCP 4.1.6 on `mcp` 2.3.0 in an isolated environment; 17 of 17 spike tests pass. The code is kept as
text in [`spikes/`](../spikes/test_spike.py.txt) so the evidence can be reproduced
(rename to `.py` and run with `pytest` in an environment with `fastmcp==4.1.6`).

| # | Result | Observed |
|---|---|---|
| Q1 | Pass | 401 with `Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource/mcp"`. The challenge carries no `error` attribute, which is correct when no credential was sent. |
| Q2 | Pass | RFC 9728 metadata with exact `resource`, `authorization_servers`, `bearer_methods_supported: ["header"]`, and a populated `scopes_supported` (today's service returns an empty list). |
| Q3 | Pass | Wrong audience, wrong issuer, expired, wrong signature and HS256 forgery all return 401. |
| Q4 | Pass, differs | Per-tool `require_scopes` works, but a caller without the scope sees the tool hidden and a call returns "Unknown tool", not today's `forbidden`. |
| Q5 | Pass | `get_access_token().claims` exposes `tenant_id`, `sub`, `azp`. |
| Q6 | Pass, with an addition | Input schemas are closed (`additionalProperties: false`); Pydantic models give closed output schemas; custom `_meta` is supported. FastMCP adds `_meta.fastmcp.tags`, which the schema-hash gate must account for. |
| Q7 | Pass | With `host_origin_protection=True`: 421 for a bad Host, 403 for a bad Origin, as today. It is off by default. |
| Q8 | Pass | Stateless JSON mode: `application/json`, no session id. |
| Q9 | Pass | Middleware sees every tool call, including denials and validation failures. Transport-level 401/421/403 stay in the existing ASGI audit middleware. |
| Q10 | Pass, with a fix | FastMCP's verifier accepted a token with no `iat`. A small `JWTVerifier` subclass that also requires `iat`, `sub`, `azp`/`client_id` and the tenant returns 401. |

**Conditions for Tasks 2 to 5**

1. Use a strict verifier subclass for `iat`, `sub`, `azp`/`client_id` and tenant (Q5, Q10).
2. Convert FastMCP's `ValidationError` to a fixed safe message in middleware; by default it leaks Pydantic internals and a documentation URL even with `mask_error_details=True`.
3. Decide how an insufficient-scope call is reported (`forbidden` today) and record it in the tool reference; add `AuthMiddleware` if the explicit error is required.
4. Teach the schema-hash gate about `_meta.fastmcp.tags`, or remove it, without changing any published hash.
5. Always set `host_origin_protection=True`, and keep the existing transport-denial audit middleware.

**Not yet tested** (Task 2 must cover): JWKS warm-up before readiness, rate limiting, worker isolation and telemetry hooks through the new tool functions, and the container build with the extra dependencies.

## Design decisions (2026-11-03)

Taken after reading the FastMCP documentation and source, and the owner's direction.

| # | Decision | Basis |
|---|---|---|
| D1 | Use FastMCP's Keycloak integration, `KeycloakAuthProvider`, but pass our strict verifier and `required_scopes=[]`. | The provider only wires the realm's JWKS URL and issuer into `RemoteAuthProvider`, which fits our Keycloak 26.8.1 (it needs 26.6 or newer). Its default `required_scopes=["openid"]` would reject our service-account tokens, and it has no `iat`, `sub`, `azp` or tenant checks. |
| D2 | Pydantic is a hidden validation layer. One registration function builds every tool from `TOOL_SPECS`; the tool function receives the existing closed, frozen input model. Authors never write validation. | Keeps the contract single-sourced, so schema hashes cannot drift. |
| D3 | Every call re-checks authorization against a policy store before anything else runs, and the default is deny. A tool or client with no grant is refused. | Matches today's dispatch re-check in `policy.invoke`, extended to a store. The check is middleware that runs before tool lookup, so a hidden tool answers "Access denied", not "Unknown tool". |
| D4 | A tool the caller cannot use is hidden from `tools/list` using the same decision as D3. | FastMCP's `on_list_tools` hook. |
| D5 | Errors carry `_meta["io.stram/error"] = {"code", "retryable"}`. Access denied is `{"code": "access_denied", "retryable": false}` with text `Access denied.` | `retryable` already exists on every error class but never reaches the client. |
| D6 | No server-side `RetryMiddleware`. | Architecture: the service never retries a data query or audit append automatically. Clients decide, using `retryable`. |
| D7 | Host and Origin protection is always on (`host_origin_protection=True`) and the allowlists come from configuration, never defaults. The operator sets the values (the owner will set them in GitHub). | Spike Q7: protection is off by default. |

**Known gap, not addressed here:** a denied call is rejected before the rate limiter runs, so repeated
denied probing is not throttled. A denial throttle is worth a separate decision.

## Task 2: Parity tests (done)

**Files:** `tests/parity/` (`harness.py`, `conftest.py`, four parity modules, `test_target_behavior.py`).

A black-box HTTP suite with real JWT verification, a registry of implementations
(`IMPLEMENTATIONS` in `harness.py`) and one fixture that runs every test once per registered
implementation. 57 tests pass on the current `sdk` implementation. They cover the 401 challenge,
resource metadata, ten token failure modes, Host and Origin, stateless JSON, the exact ten-tool contract
against `contracts/tool_schemas.json`, per-tool scope denial, safe errors, and one audit record per call.

Four target-behavior tests (D3 to D5) are skipped until the FastMCP implementation is registered.

Effectiveness check: removing the scope check made the authorization tests fail; disabling Host and
Origin protection made the transport tests fail. Both changes were reverted.

Parity found one fact worth recording: a token with an empty `scope` claim is rejected at the transport
(401), before any tool is reached.

## Task 3: Dependency change (done)

`fastmcp==4.1.6` added with `uv add`. The lock gained 25 packages and changed no existing version
(`mcp` stays 2.3.0). `pip-audit` reports no known vulnerabilities, the license scan covers 130 packages, and
the container's dependency export includes FastMCP and `httpx2` (the export excludes only `httpx`,
`confluent-kafka` and `psycopg-binary`, none of which FastMCP needs).

## Task 4: FastMCP implementation (done)

New modules, each small and following `ENGINEERING.md` 3.1, all registered in the layer contract:

| Module | Purpose |
|---|---|
| `access_policy` | Default-deny policy store; one decision for listing and for calling |
| `denial_throttle` | Bounded per-caller sliding window that blocks repeated denials |
| `request_context` | Request and trace identifiers shared across layers |
| `fastmcp_audit` | One terminal audit record for calls refused before the worker |
| `fastmcp_auth` | Strict verifier, Keycloak provider, JWKS warm-up |
| `fastmcp_tools` | Tools that publish the reviewed schemas and validate with the closed models |
| `fastmcp_middleware` | Telemetry, safe error shaping, access control |
| `request_limits` | Request-body size cap |
| `fastmcp_server` | Assembly, health routes, startup and shutdown |

Spike conditions resolved: strict verifier (1); validation errors replaced by a fixed message (2);
insufficient scope is reported as `Access denied.` (3); schemas are published exactly, with
`dereference_schemas=False` so `$ref` definitions are not inlined (4); Host and Origin protection always on
(5). Design decisions D1 to D7 are implemented.

Findings made while building, now covered by tests:

- FastMCP wraps every exception raised in a tool as a generic `ToolError`; the error layer follows the
  cause chain to find the classified error.
- The framework logs the attacker-chosen audience of a rejected token and full tracebacks; its logger is
  silenced and a test proves nothing sensitive reaches any log (the test fails if the silencing is removed).
- A real tool with no grant is a denial, not an unknown tool; "unknown" means absent from the catalogue.
- A failed JWKS warm-up after the worker started would have leaked the worker; the lifespan now cleans up.

## Task 5: Switch and rollback (code done; live switch pending)

`app.create_from_env` builds the FastMCP server. The staging acceptance runner passes over real TLS, and the
official MCP client completes a session. The previous server was then deleted (after the user's decision) and
kept on the `archive/sdk-server` branch. Not done, because it needs authorization: building and approving the
image digest and running staging acceptance against the real Keycloak and lake.
