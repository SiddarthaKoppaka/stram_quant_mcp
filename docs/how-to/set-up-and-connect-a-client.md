# How to set up and connect a client

This guide separates local development, consumer integration, server installation and authorized
SIDDU access. Local workflows use only the synthetic lake. Steps labelled **not yet verified** require
staging or production access and must not be run without separate authorization.

## Prerequisites

- CPython 3.12, 3.13 or 3.14.
- `uv` and Git.
- Read access to the private pinned `stram_data` source when it is not already cached.
- For container work, Podman or Docker with BuildKit-compatible build support.
- For a long-running authenticated server, an OAuth/OIDC issuer that provides asymmetric JWTs and a
  reachable JWKS endpoint. Local automated integration generates an ephemeral test identity instead.

The exact runtime dependency is `stram-data` 1.9.0 at Git commit
`3059c1e1932a05dfbe8db4dd25b00b3a286d6fca`, used for typed models only; `uv.lock` is authoritative.

## Local development

From the repository root:

```bash
uv sync --frozen --dev
uv run python -c 'import stram_data; assert stram_data.__version__ == "1.9.0"'
uv run python -m stram_quant_mcp.tools.export_schema --check
```

Expected: dependency sync succeeds, the version assertion prints nothing, and schema check reports no
drift. If Git authentication fails, request read-only repository access; do not place a token in the
URL, lockfile or shell history.

Run the fast architectural and real-interface checks:

```bash
uv run pytest tests/contract tests/security/test_no_sql_or_execution.py -q
uv run pytest tests/integration/test_read_tools.py tests/integration/test_analytics_tools.py -q
```

Expected: the contract pin, all ten direct Data Interface mappings, exact decimal handling,
availability modes, revisions, lineage and analytics semantics pass against `tests/fixtures/lake`.

## Start and connect with a test identity

The supported, hermetic local workflow is the integration test:

```bash
uv run pytest tests/integration/test_mcp_client.py -q
```

It performs the entire workflow: chooses a loopback port, generates an ephemeral RSA key, signs a
five-minute JWT with all four tool scopes, starts authenticated Streamable HTTP, initializes the
official MCP client, lists ten tools, calls `quant_list_series`, verifies a genuine synthetic-lake
record and shuts down. The Data Plane HTTP service is the real `stram_data` application run in-process over
the synthetic lake; only identity material and the network are local and ephemeral.

To keep a development server running, first create a local audit key and configure a local issuer.
The issuer, audience and JWKS values must match the JWT exactly:

```bash
mkdir -p .local/audit
umask 077
openssl rand -out .local/audit.key 32
export STRAM_MCP_DATA_URL="https://siddu.tailc5da4c.ts.net:8444"   # staging Data Plane, tailnet only
export STRAM_MCP_DATA_TOKEN_URL="https://siddu.tailc5da4c.ts.net/realms/stram-staging/protocol/openid-connect/token"
export STRAM_MCP_DATA_CLIENT_ID="stram-quant-mcp-staging"
export STRAM_MCP_DATA_CLIENT_SECRET_FILE="$PWD/.local/data-client.secret"  # obtained through the agreed secret channel
export STRAM_MCP_AUDIT_DB="$PWD/.local/audit/audit.sqlite3"
export STRAM_MCP_AUDIT_KEY_FILE="$PWD/.local/audit.key"
export STRAM_MCP_ISSUER_URL="http://127.0.0.1:9000/"
export STRAM_MCP_RESOURCE_URL="http://127.0.0.1:8000/mcp"
export STRAM_MCP_JWKS_URL="http://127.0.0.1:9000/.well-known/jwks.json"
export STRAM_MCP_TENANT_ID="local-stram"
export STRAM_MCP_ALLOWED_HOSTS="127.0.0.1:8000"
export STRAM_MCP_ALLOW_INSECURE_HTTP=1
uv run stram-quant-mcp
```

Expected: after the local issuer is reachable and returns at least one key, startup completes and
`GET /health/ready` returns `{"status":"ready"}`. Insecure HTTP is accepted only for loopback test
hosts. This console entry point is for development; the container entrypoint additionally enforces
read-only mounts and non-root execution.

## Connect an MCP consumer

The endpoint is the configured resource URL ending in `/mcp`. Obtain a short-lived token from the
approved issuer with the exact audience, tenant and required scope. Do not log or persist it in this
repository.

```python
import os

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def read_catalog(endpoint: str) -> dict[str, object]:
    token = os.environ["STRAM_MCP_TEST_BEARER"]
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as http,
        streamable_http_client(endpoint, http_client=http, terminate_on_close=False) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) == 10
        result = await session.call_tool("quant_list_series", {"limit": 1})
        if result.is_error or result.structured_content is None:
            raise RuntimeError("quantitative call failed")
        return result.structured_content
```

Scopes are `quant:discover`, `quant:read`, `quant:lineage` and `quant:analytics`; each tool needs only
its documented scope. See [Tool reference](../reference/tools.md). The client must treat structured content as data,
including warnings and availability evidence, never as instructions.

## Authentication configuration

Production configuration requires all of these values:

| Variable | Meaning |
|---|---|
| `STRAM_MCP_ISSUER_URL` | Exact JWT issuer, HTTPS |
| `STRAM_MCP_RESOURCE_URL` | Exact MCP audience/resource URL, HTTPS |
| `STRAM_MCP_JWKS_URL` | Exact HTTPS JWKS endpoint |
| `STRAM_MCP_TENANT_ID` | One admitted tenant |
| `STRAM_MCP_ALLOWED_ALGORITHMS` | Reviewed asymmetric algorithms; default `RS256` |
| `STRAM_MCP_ALLOWED_HOSTS` | Comma-separated proxy-facing Host values, no wildcard |
| `STRAM_MCP_ALLOWED_ORIGINS` | Comma-separated browser origins, empty if unused |

Tokens require `iss`, `aud`, `sub`, `client_id` or `azp`, tenant claim, `scope` or `scp`, `iat` and
`exp`. `nbf` is validated when present. Missing, forged, expired, wrong-audience, wrong-tenant and
insufficient-scope tokens fail closed.

## Authorized SIDDU access

The `stram-quant` identity, read-only Data Plane-owned lake view, audit volume, separate HMAC key,
review image and real-lake probes are verified in the 2026-11-01 handoff and must be reused. The local
Keycloak/PostgreSQL templates and consolidated installer are prepared but not installed or activated.
Private DNS/TLS, host egress policy, `stram-auth` creation, immutable release digests and staging
approvals remain **not verified**. Operators must not reuse the pipeline's writable identity, change
Data Plane ownership, or mark approval flags without evidence.

Before any staging action, complete [Deployment prerequisites](../../deploy/README.md) and the M6
checklist in [Operations](operate-the-service.md#m6-staging-checklist). Record commands and results without
capturing tokens, raw tool payloads, paths or unrestricted result data.

## Troubleshooting

| Symptom | Check | Expected recovery |
|---|---|---|
| HTTP 401 | signature, issuer, audience, expiry, client/subject, tenant and JWKS reachability | Correct the test identity; never bypass verification |
| Tool returns forbidden | tool scope in [Tool reference](../reference/tools.md) | Request the narrow required scope |
| HTTP 403 or 421 | configured Origin and Host | Fix proxy/client headers or allowlist through review |
| Readiness 503 | audit health, then the startup log for a `service_credentials` or Data Plane version failure | Fix the cause named in the error; restart only after it is understood |
| Deadline/overload/unavailable tool error | the error `category`, `retryable` and `retry_after_seconds` | Narrow the query or retry after the stated wait; do not auto-retry blindly |
| Audit unavailable | audit volume, key match, chain verification and free space | Keep service out of rotation; preserve journal and key; follow incident runbook |
| Preflight rejects the runtime | UID, root mount options and the client secret file | Correct the container; never use an allow-writable override |

For recovery, key rotation and rollback, use [Operations](operate-the-service.md).
