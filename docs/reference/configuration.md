# Configuration reference

The service reads its configuration once at startup from environment variables and fails closed if a
required value is missing or invalid. Secrets are read from files, never from variables. The source of
truth is [`settings.py`](../../src/stram_quant_mcp/settings.py).

## Required variables

| Variable | Meaning |
|---|---|
| `STRAM_MCP_DATA_URL` | Base URL of the Data Plane service, for example `https://siddu.tailc5da4c.ts.net:8444` |
| `STRAM_MCP_DATA_TOKEN_URL` | Keycloak token endpoint used for the service's `client_credentials` token |
| `STRAM_MCP_DATA_CLIENT_ID` | Keycloak client id of this service |
| `STRAM_MCP_DATA_CLIENT_SECRET_FILE` | File holding the client secret; mounted, never an environment value |
| `STRAM_MCP_AUDIT_DB` | Path of the SQLite audit journal |
| `STRAM_MCP_AUDIT_KEY_FILE` | File holding the audit HMAC key, at least 32 bytes |
| `STRAM_MCP_ISSUER_URL` | Exact token issuer URL |
| `STRAM_MCP_RESOURCE_URL` | Exact MCP resource URL; also the required token audience |
| `STRAM_MCP_JWKS_URL` | Exact JWKS URL used to verify signatures |
| `STRAM_MCP_TENANT_ID` | The single admitted tenant |
| `STRAM_MCP_ALLOWED_HOSTS` | Comma-separated Host allowlist; wildcards are rejected |

## Optional variables

| Variable | Default | Meaning |
|---|---|---|
| `STRAM_MCP_ALLOWED_ORIGINS` | empty | Comma-separated browser origins; empty for non-browser clients |
| `STRAM_MCP_ALLOWED_ALGORITHMS` | `RS256` | Reviewed asymmetric JWT algorithms |
| `STRAM_MCP_JWT_CLOCK_SKEW_SECONDS` | `30` | Allowed clock skew when evaluating `iat`, `exp`, and optional `nbf`; 0 to 300 seconds |
| `STRAM_MCP_JWT_MAX_LIFETIME_SECONDS` | `900` | Maximum permitted `exp - iat` access-token lifetime; greater than 0 and at most 3600 seconds |
| `STRAM_MCP_ALLOW_INSECURE_HTTP` | unset | `1` permits plain HTTP, for loopback tests only |
| `STRAM_MCP_IMAGE_DIGEST` | `development` | Image digest recorded in audit and telemetry |
| `STRAM_MCP_OTLP_ENDPOINT` | unset | OpenTelemetry collector URL |
| `STRAM_MCP_DATA_MAX_CONCURRENT` | `4` | Concurrent Data Plane calls; extra calls wait up to 2 s, then fail as `overloaded` |
| `STRAM_MCP_DATA_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout for the Data Plane and identity provider |
| `STRAM_MCP_DATA_BREAKER_THRESHOLD` | `5` | Consecutive Data Plane failures that open the circuit breaker |
| `STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS` | `15` | How long the breaker sheds load before one probe call |
| `STRAM_MCP_BACKEND_SHUTDOWN_SECONDS` | `5` | Drain budget on shutdown |
| `STRAM_MCP_DENIAL_LIMIT` | `5` | Denied calls inside the window after which a caller is blocked |
| `STRAM_MCP_DENIAL_WINDOW_SECONDS` | `60` | Length of the sliding window for counting denials |
| `STRAM_MCP_BIND_HOST` | `127.0.0.1` | `127.0.0.1` or `0.0.0.0` (container only) |
| `STRAM_MCP_BIND_PORT` | `8000` | Listen port, 1 to 65535 |

Defaults that affect security or correctness need review before they change; record the change in
[Plans](../../PLANS.md). Field inventories (not ready-to-run files) are in
[`service.env.example`](../../deploy/service.env.example) and
[`runtime.env.example`](../../deploy/runtime.env.example).

## Token requirements

Access tokens must carry `iss`, `aud`, `sub`, `client_id` or `azp`, the tenant claim (`tenant_id`),
`scope` or `scp`, numeric `iat`, and numeric `exp`. They are rejected when expired, issued too far in
the future, have a non-positive or over-limit lifetime, or carry an `nbf` that is too far in the
future. `nbf` is optional, but when present must be numeric and valid under the configured clock
skew.

## Qual client policy

These clients exist in the `stram-staging` realm for the second MCP. Every token carries the Qual
audience and the `tenant_id` claim; no `qual:*` scope is granted unless the client requests it.

| Client | Scopes it may request |
|---|---|
| `stram-qual-smoke` | `qual:discover`, `qual:read`, `qual:analytics` |
| `stram-qual-text` | `qual:discover`, `qual:text` |
| `stram-qual-discovery-only` | `qual:discover` |

A request for any other scope is rejected by Keycloak. To change this table, edit `CLIENTS` in
[the provisioning tool](../../tools/provision_qual_clients.py) and follow
[how to provision the Qual clients](../how-to/provision-qual-clients.md).
