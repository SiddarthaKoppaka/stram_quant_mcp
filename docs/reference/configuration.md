# Configuration reference

The service reads its configuration once at startup from environment variables and fails closed if a
required value is missing or invalid. Secrets are read from files, never from variables. The source of
truth is [`settings.py`](../../src/stram_quant_mcp/settings.py).

## Required variables

| Variable | Meaning |
|---|---|
| `STRAM_MCP_PARQUET_ROOT` | Lake root directory; `/lake` in the container |
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
| `STRAM_MCP_ALLOW_INSECURE_HTTP` | unset | `1` permits plain HTTP, for loopback tests only |
| `STRAM_MCP_IMAGE_DIGEST` | `development` | Image digest recorded in audit and telemetry |
| `STRAM_MCP_OTLP_ENDPOINT` | unset | OpenTelemetry collector URL |
| `STRAM_MCP_WORKER_MAX_RESTARTS` | `3` | Consecutive worker replacements before readiness fails |
| `STRAM_MCP_WORKER_RESTART_BACKOFF_SECONDS` | `0.25` | Delay between replacements |
| `STRAM_MCP_WORKER_SHUTDOWN_SECONDS` | `5` | Drain budget on shutdown |
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
`scope` or `scp`, `iat` and `exp`. `nbf` is checked when present.

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
