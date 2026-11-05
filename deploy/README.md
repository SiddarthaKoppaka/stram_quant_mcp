# SIDDU private staging deployment

The supported SIDDU entrypoint is [`stram_quant_siddu.sh`](stram_quant_siddu.sh). It consolidates the
previous `/home/siddu/Downloads/stram_quant_siddu.sh` workflow and keeps its `prepare`, `activate` and
`status` commands compatible. Run the repository copy during review; install it over the Downloads
copy only as an explicit operator change. Individual actions do not auto-approve evidence or enable
units at boot. The separately reviewed [`complete_siddu_staging.sh`](complete_siddu_staging.sh)
records technical evidence only after its live check and enables persistence last. Neither workflow
opens a public listener, alters the real lake, or enables a GitHub runner.

The target has two security identities:

- `stram-quant` owns only the Quant rootless Podman store, audit volume, HMAC secret and the Data Plane
  client secret. It has no lake mount: it reads quantitative data only through the Data Plane HTTP
  service. Its MCP listener is `127.0.0.1:8800`.
- `stram-auth` owns an independent rootless Podman store, internal network, PostgreSQL volume and
  Keycloak secrets. It receives no lake, audit volume or audit key. Keycloak HTTP and management ports
  publish only on loopback for the approved private TLS proxy.

The identities do not share a Podman network. Quant reaches only the client-facing HTTPS issuer/JWKS
through an administrator-owned egress allowlist. A Podman `--internal` network isolates Keycloak and
PostgreSQL from outbound traffic; it does not substitute for the Quant host firewall.

## Safe command sequence

Read-only inspection is always the first step:

```bash
bash deploy/stram_quant_siddu.sh doctor
sudo bash deploy/stram_quant_siddu.sh doctor
```

The second form can inspect protected service-user state but does not mutate it. `PASS` means the
specific check ran; `NOT VERIFIED` and `BLOCKED` must remain open until evidence exists.

Provision the independently isolated auth identity, import the reviewed image archives into the two
rootless stores, then reuse and re-check the existing audit volume, audit key and Data Plane client secret:

```bash
sudo bash deploy/stram_quant_siddu.sh provision-auth
sudo bash deploy/stram_quant_siddu.sh import-images
sudo bash deploy/stram_quant_siddu.sh prepare
```

`prepare` repeats the non-destructive isolation, preflight and live Data Plane probes and creates missing
configuration templates only. It never regenerates an existing audit HMAC key or journal.

`provision-auth` creates the non-login `stram-auth` identity with a non-overlapping 65536-ID subordinate
UID/GID range and lingering, blocks it from traversing the Quant lake parent, and creates the separate
non-internal Quant network whose egress is controlled by the host firewall. Place reviewed digest
references and exact private URLs in `/etc/stram-quant/auth.env`. Build optimized Keycloak 26.8.1 from
[`keycloak/Containerfile`](keycloak/Containerfile) and hardened PostgreSQL 17.11 from
[`postgres/Containerfile`](postgres/Containerfile) using their exact base digests. Retain each SBOM,
local provenance manifest and scan. Local staging references are allowed only as retained
`localhost/...@sha256:...` digests; production requires the approved private registry.

After those external inputs are approved:

```bash
sudo bash deploy/stram_quant_siddu.sh auth-prepare
```

This command is idempotent. It creates missing random secrets once, imports them into the `stram-auth`
Podman secret store, creates the internal auth network and PostgreSQL volume, copies the reviewed realm
template, wires non-secret MCP OAuth settings, and renders stopped/disabled user Quadlets. Existing
secrets are never replaced. A changed realm template or conflicting live setting blocks instead of
silently overwriting state.

Service activation is a separate authorized operation. The root-owned approval file must contain real
evidence rather than optimistic flags:

```bash
sudo bash deploy/stram_quant_siddu.sh auth-start
sudo bash deploy/stram_quant_siddu.sh auth-verify
sudo bash deploy/stram_quant_siddu.sh install-egress
sudo bash deploy/stram_quant_siddu.sh prepare-acceptance
sudo bash deploy/stram_quant_siddu.sh activate
```

`auth-start` starts, but does not boot-enable, PostgreSQL and Keycloak. `auth-verify` requires trusted
HTTPS discovery to match the configured issuer/JWKS both on the host and from the MCP container.
`install-egress` installs a persistent nftables output chain keyed to the `stram-quant` host UID. A
static Quadlet host mapping removes the need for DNS: only TCP/443 to SIDDU's verified Tailscale IPv4
is accepted; every other DNS, IPv4 and IPv6 destination is rejected. `prepare-acceptance` generates
four bounded calls through `stram_data.data_interface` and creates genuine temporary
Keycloak-signed negative JWT evidence, deleting its temporary clients and second realm afterward.
`activate` requires the approved Quant digest, SBOM/local-provenance/scan review, private TLS/Host
policy, egress proof and OIDC/JWKS proof. Post-activation audit and rollback evidence is deliberately
not a circular precondition. The MCP Quadlet remains loopback-only and boot-disabled at this stage.

Create `/etc/stram-quant/staging-calls.json` from
[`staging-calls.example.json`](staging-calls.example.json) using genuine, non-sensitive Stock series and
event identifiers, or use `prepare-acceptance`. Restricted Keycloak-signed negative tokens are stored
under `/etc/stram-quant/negative-tokens/`; the wrong-issuer token includes its captured alternate-realm
JWKS sidecar. The verifier checks every RS256 signature and intended wrong condition before accepting
the HTTP denial as evidence. Regenerate this short-lived evidence for later acceptance runs. Then run:

```bash
sudo bash deploy/stram_quant_siddu.sh verify
sudo bash deploy/stram_quant_siddu.sh reliability-drill
sudo bash deploy/stram_quant_siddu.sh backup-audit
sudo bash deploy/stram_quant_siddu.sh verify-backup
sudo bash deploy/stram_quant_siddu.sh install-backup-timer
sudo bash deploy/stram_quant_siddu.sh status
```

`verify` checks discovery/JWKS and certificate trust, exact positive claims, missing/bad/wrong/expired
token denial, Host/Origin denial, insufficient scope, exactly ten tools, representative authenticated
authenticated calls, a live service-token and Data Plane link check, and denied DNS/IPv4/IPv6 egress. It prints no token or tool result.
`backup-audit` uses SQLite online backup against the live WAL journal, verifies the copy with the
matching HMAC key, makes it root-owned/read-only, and records a separate host-keyed checkpoint.
`verify-backup` independently verifies the checkpoint signature, backup digest and complete audit
chain. The persistent daily timer applies a documented 30-day private-staging retention window.
`reliability-drill` blocks the MCP user's route to the Data Plane port with a temporary firewall table
to prove classified, retryable failures, that readiness stays true, and circuit-breaker recovery, always
removes the table, then performs a controlled bounded service restart.

Rollback is also separately gated and never touches the Data Plane or replaces the live audit journal:

```bash
sudoedit /etc/stram-quant/m6-approval.env
sudo bash deploy/stram_quant_siddu.sh rollback
sudo bash deploy/stram_quant_siddu.sh restore-approved
sudo bash deploy/stram_quant_siddu.sh enable-services
```

The operator must set a retained `ROLLBACK_IMAGE_REF` digest and `ROLLBACK_AUTHORIZED=yes` for the
specific event. The command first takes a verified audit backup/checkpoint, probes the rollback image
(container isolation, preflight and the live Data Plane link), preserves the current Quadlet, restarts with the previous digest and
requires readiness. The genuine retained approved artifact is restored and re-verified after the
drill. `enable-services` refuses to add Quadlet boot persistence until live acceptance, backup,
checkpoint and rollback/recovery gates are all recorded as verified.

## Shared Keycloak: discovery routes and the second MCP

Quant's audience, clients and scopes are unchanged. Two additive steps are staged and **not yet applied to the live host**.

**1. Discovery routes (fixes OAuth discovery for Quant).** The app already serves RFC 9728 metadata at
`/.well-known/oauth-protected-resource/mcp`, but Tailscale Serve on `:443` proxied only `/mcp` and
`/realms`, so the path returned 404. After reviewing, run:

```bash
sudo bash deploy/stram_quant_siddu.sh discovery-routes status
sudo STRAM_CONFIRM_DISCOVERY_ROUTES=yes bash deploy/stram_quant_siddu.sh discovery-routes apply
sudo STRAM_CONFIRM_DISCOVERY_ROUTES=yes bash deploy/stram_quant_siddu.sh discovery-routes remove  # rollback
```

`apply` and `remove` touch only the two `/.well-known/` handlers. `verify` reports their presence and the
acceptance runner checks the 401 challenge, resource metadata and RFC 8414 server metadata.

**2. Second MCP Keycloak clients.** Audience `https://siddu.tailc5da4c.ts.net:8443/mcp`; scopes
`qual:discover`, `qual:read`, `qual:analytics`, `qual:text`. `qual:text` is optional-only and requestable by
`stram-qual-text` alone. Plan first, then apply and verify claims:

```bash
.venv/bin/python tools/provision_qual_clients.py --dry-run --public-url "$KEYCLOAK_PUBLIC_URL" \
  --realm stram-staging --qual-resource "$QUAL_RESOURCE_URL" --quant-resource "$MCP_RESOURCE_URL" \
  --tenant local-stram --bootstrap-password-file "$BOOTSTRAP_FILE" --secret-dir "$QUAL_SECRET_DIR"
```

Re-run without `--dry-run --verify` only after approval. Client secrets are created once as owner-only files
and are never printed; hand them to the other team through the agreed secret channel, never chat or Git.

**Activation order (nothing below is done yet).** The other team must first confirm readiness in writing.
Then: apply discovery routes, provision clients, verify claims, and only then add the `:8443` Serve entry
and let the second MCP use the new audience. Roll back by removing the `:8443` entry, then the discovery
routes; the provisioning script is additive, so disable the `stram-qual-*` clients in Keycloak if needed.

## Keycloak and proxy invariants

The realm import is intentionally narrow: `RS256`, five-minute access tokens, no registration or
password grant, a confidential service-account smoke client, a discovery-only negative client, exact
URL audience, exact `tenant_id`, and four independent optional Quant scopes. Secrets enter realm
import through Podman secret-backed environment variables and never appear in Git or CLI arguments.
Existing realms are skipped by Keycloak; configuration drift is reported and must be reviewed rather
than overwritten online.

Keycloak runs production `start --optimized`, never `start-dev`. Its full public and admin HTTPS URLs
are fixed, proxy headers are accepted only because the private proxy must overwrite them, backchannel
URLs are not dynamic, health/metrics stay on the loopback management port, and the proxy must block
master-realm and administration paths from the frontend hostname. Keycloak's documented admin
hostname alone does not enforce that path restriction.

If a private CA is selected, add its CA certificate to a reviewed immutable Quant image and set the
host-side `STRAM_PRIVATE_CA_FILE` for the verifier. Never disable TLS validation. Tailscale DNS/HTTPS
and a private CA are both viable only after the operator chooses one consistent hostname topology
reachable by Keycloak, the MCP container and authorized clients.

## Release artifacts and limits

The retained `localhost/stram-quant-mcp:review@sha256:...` image is the genuine rollback artifact.
Activation accepts only an immutable reference ending in `@sha256:<64 lowercase hex>`. Retain the
Quant, Keycloak and PostgreSQL SBOMs, local provenance manifest, vulnerability results, base digests,
archives and rollback digest outside Git. This staging build does not claim a registry signature or
independent security approval. The reviewed PyArrow and time-bounded Keycloak/Guava exceptions in
[`SECURITY_EXCEPTIONS.md`](../docs/reference/security-exceptions.md) remain explicit release exceptions.

Private staging is not production approval. Production still requires owners for DNS/TLS, firewall,
identity, audit/checkpoint retention, alerting/SLOs, image review, rollback and the upstream lake backup
risk. See the full [operations runbook](../docs/how-to/operate-the-service.md#m6-staging-checklist).
