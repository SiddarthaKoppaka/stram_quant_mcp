# Deploy the HTTP gateway build on SIDDU

This guide moves a SIDDU staging host from the earlier build, which mounted the lake and ran a seccomp
worker, to the build that calls the Data Plane service over HTTP. It is written for the operator who
holds `sudo` on SIDDU. Every step that changes the host needs the separate authorization recorded in
`/etc/stram-quant/m6-approval.env`; nothing here is run by the repository's tests or CI.

## What changes on the host

| Item | Before | After |
|---|---|---|
| Lake | Bind-mounted read-only into the container | Not mounted. The Data Plane service reads it |
| Data access | In-process `DataInterface` in a worker | HTTPS calls to the Data Plane service on port 8444 |
| Identity to the Data Plane | none | This service's own `client_credentials` token (Keycloak client `stram-data-mcp-staging`) |
| Secrets in the container | `audit-key` | `audit-key` and `data-client-secret` |
| Egress for the `stram-quant` user | Keycloak and the private host on 443 | The same, plus the Data Plane port (8444) on the private host |
| Probes | `worker-network-probe`, `worker-interface-probe` | `preflight` and `data-plane-probe` |

## Prerequisites

- The image digest built from the merged change, retained in the `stram-quant` Podman store, and the
  previous digest kept as the rollback target.
- The Data Plane service running and reachable on the private host (`/healthz` returns `ok`).
- The Keycloak client `stram-data-mcp-staging` with a secret, delivered through your agreed secret
  channel. It is not in Bitwarden yet. Never paste it into chat, Git or an environment file.
- A reviewed grant of the client's scopes: `series:read`, `derived:read`, `analytics:read`,
  `catalog:read`, `lineage:read`, and `hindsight:read` as an optional scope only. The Quant MCP needs no
  `news:*` or `market:*` scope.

## 1. Put the client secret in a Podman secret

```bash
# As root, from the secret file delivered to the host. The value is never printed.
tr -d '\r\n' < /etc/stram-quant/secrets/data-client-secret \
  | sudo -u stram-quant podman secret create --replace stram-quant-data-client-secret -
```

The deploy script mounts it read-only into the container as `/run/secrets/data-client-secret`, owned by
UID 10001 with mode 0400.

## 2. Update the service configuration

`wire_service_config` (run by `prepare`) writes these non-secret values to `service.env`; they replace
the `STRAM_MCP_PARQUET_ROOT` and `STRAM_MCP_WORKER_*` entries:

| Variable | Value |
|---|---|
| `STRAM_MCP_DATA_URL` | `https://<private host>:8444` |
| `STRAM_MCP_DATA_TOKEN_URL` | `<issuer>/protocol/openid-connect/token` |
| `STRAM_MCP_DATA_CLIENT_ID` | `stram-data-mcp-staging` (override with `STRAM_QUANT_DATA_CLIENT_ID`) |
| `STRAM_MCP_DATA_CLIENT_SECRET_FILE` | `/run/secrets/data-client-secret` |
| `STRAM_MCP_DATA_MAX_CONCURRENT`, `STRAM_MCP_DATA_BREAKER_THRESHOLD`, `STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS` | `4`, `5`, `15` |

`service.env` must hold no secret value. The activation check rejects any key containing `SECRET`,
`TOKEN` or `PASSWORD`, except a `*_SECRET_FILE` path or a `*_TOKEN_URL`. Its other required values are
checked exactly, so a drifted value fails with `BLOCKED: incorrect <name>` before anything starts.

## 3. Allow exactly one more destination

`install-egress` adds one rule for the `stram-quant` user to the private host's Data Plane port, in
addition to the existing HTTPS rule, and keeps the final reject rule:

```bash
sudo bash deploy/stram_quant_siddu.sh install-egress
```

The script then confirms both allow rules and the default reject exist. Override the address or port
with `STRAM_QUANT_DATA_IPV4` or `STRAM_QUANT_DATA_PORT` only if the Data Plane is not on the private host.

## 4. Probe, then activate

```bash
sudo bash deploy/stram_quant_siddu.sh prepare      # isolation, preflight and live Data Plane probes
sudoedit /etc/stram-quant/m6-approval.env          # set APPROVED_IMAGE_REF to the new digest
sudo bash deploy/stram_quant_siddu.sh activate
sudo bash deploy/stram_quant_siddu.sh verify
```

Updating `APPROVED_IMAGE_REF` and the gate flags is a separate approval. `verify` compares the running
image with `APPROVED_IMAGE_REF`, so it stops at the first check until that value names the new digest.
The `data-plane-probe` command inside the running container must print `{"status":"data_plane_ok"}`.

## 5. Failure drill

```bash
sudo bash deploy/stram_quant_siddu.sh reliability-drill
```

The drill adds a temporary firewall table that rejects only the `stram-quant` user's traffic to the Data
Plane port. It expects tool calls to fail with classified, retryable `unavailable` errors, readiness to
stay true, and normal calls to succeed again once the table is removed and the circuit breaker's
cooldown has passed. The table is removed even if the drill fails.

## Roll back

Keep the earlier unit, `service.env` and egress file next to their originals (`*.before-gateway`). To
return to the previous build, restore them, then restart the unit:

```bash
sudoedit /etc/stram-quant/m6-approval.env   # ROLLBACK_IMAGE_REF and ROLLBACK_AUTHORIZED=yes
sudo bash deploy/stram_quant_siddu.sh rollback
sudo bash deploy/stram_quant_siddu.sh restore-approved
```

The earlier build needs its lake mount and worker settings restored with it; the data client secret and
the extra egress rule can stay in place.

## Diagnose a failing call

Read the `category` in the tool error and the audit outcome.

| Category | Likely cause on SIDDU | Action |
|---|---|---|
| `service_credentials` | Wrong secret, audience or scopes on the Keycloak client | Check the client in Keycloak and the mounted secret file; do not restart in a loop |
| `data_plane_unavailable` | Data Plane down, or the egress rule is missing | Check the service on port 8444 and `nft list chain inet stram_quant output` |
| `data_plane_error` | Data Plane fault or an incompatible version | Capture the MCP and Data Plane request ids; check the Data Plane is 1.9 or later |
| `deadline` | A slow Data Plane query | Narrow the query before raising any deadline |

See [Tool reference](../reference/tools.md#error-replies) for the full error table and
[Architecture](../explanation/architecture.md) for the boundaries.

## Still open

- The client secret is not yet in Bitwarden.
- This service calls the Data Plane as itself; end-user identity needs the STRAM API team's token
  exchange.
- Production identity, TLS hostname and approval are not decided. This guide covers staging only.
