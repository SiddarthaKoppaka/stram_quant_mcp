# SIDDU private staging deployment

The supported SIDDU entrypoint is [`stram_quant_siddu.sh`](stram_quant_siddu.sh). It consolidates the
previous `/home/siddu/Downloads/stram_quant_siddu.sh` workflow and keeps its `prepare`, `activate` and
`status` commands compatible. Run the repository copy during review; install it over the Downloads
copy only as an explicit operator change. The workflow does not auto-approve evidence, enable units at
boot, open a public listener, alter the real lake, or enable the disabled GitHub runner.

The target has two security identities:

- `stram-quant` owns only the Quant rootless Podman store, read-only lake view, audit volume and HMAC
  secret. Its MCP listener is `127.0.0.1:8800`.
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

The existing verified lake, audit volume, audit key and review image are reused:

```bash
sudo bash deploy/stram_quant_siddu.sh prepare
```

`prepare` repeats the non-destructive real-lake and worker probes and creates missing configuration
templates only. It never regenerates an existing audit HMAC key or journal.

Before `auth-prepare`, an operator must approve and create the non-login `stram-auth` identity with
subordinate UID/GID ranges and lingering, then place reviewed digest references and exact private
URLs in `/etc/stram-quant/auth.env`. Build the optimized Keycloak image from
[`keycloak/Containerfile`](keycloak/Containerfile) using the exact base digest in `auth.env`; retain its
SBOM, provenance and scan, publish it to the approved private registry, and replace
`KEYCLOAK_IMAGE_REF` with its immutable registry digest. Do the same digest review for PostgreSQL.

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
sudo bash deploy/stram_quant_siddu.sh activate
```

`auth-start` starts, but does not boot-enable, PostgreSQL and Keycloak. `auth-verify` requires trusted
HTTPS discovery to match the configured issuer/JWKS both on the host and from the MCP container.
`activate` additionally requires the approved Quant registry digest, SBOM/provenance/scan review,
private TLS/Host policy, IPv4/IPv6/DNS egress proof, OIDC proof, audit custody and rollback evidence.
The MCP Quadlet remains loopback-only and boot-disabled.

Create `/etc/stram-quant/staging-calls.json` from
[`staging-calls.example.json`](staging-calls.example.json) using genuine, non-sensitive Stock series and
event identifiers. Provide restricted, Keycloak-signed negative tokens under
`/etc/stram-quant/negative-tokens/` only for the acceptance run; the verifier cryptographically checks
their current realm signing key and intended wrong condition before accepting the HTTP denial as
evidence. Then run:

```bash
sudo bash deploy/stram_quant_siddu.sh verify
sudo bash deploy/stram_quant_siddu.sh backup-audit
sudo bash deploy/stram_quant_siddu.sh status
```

`verify` checks discovery/JWKS and certificate trust, exact positive claims, missing/bad/wrong/expired
token denial, Host/Origin denial, insufficient scope, exactly ten tools, representative authenticated
real-lake calls, worker seccomp and denied DNS/IPv4/IPv6 egress. It prints no token or tool result.
`backup-audit` uses SQLite online backup against the live WAL journal, verifies the copy with the
matching HMAC key, makes it root-owned/read-only, and records a separate host-keyed checkpoint.

Rollback is also separately gated and never changes the lake or replaces the live audit journal:

```bash
sudoedit /etc/stram-quant/m6-approval.env
sudo bash deploy/stram_quant_siddu.sh rollback
```

The operator must set a retained `ROLLBACK_IMAGE_REF` digest and `ROLLBACK_AUTHORIZED=yes` for the
specific event. The command first takes a verified audit backup/checkpoint, probes the rollback image
against the real read-only lake, preserves the current Quadlet, restarts with the previous digest and
requires readiness.

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

The existing `localhost/stram-quant-mcp:review` image and image ID are probe inputs only. Activation
accepts only a registry reference ending in `@sha256:<64 lowercase hex>`. Retain the Quant and optimized
Keycloak SBOMs, provenance, vulnerability results, base digests and rollback digests outside the
repository. The reviewed PyArrow exception in
[`SECURITY_EXCEPTIONS.md`](../docs/SECURITY_EXCEPTIONS.md) remains in force and cannot be broadened.

Private staging is not production approval. Production still requires owners for DNS/TLS, firewall,
identity, audit/checkpoint retention, alerting/SLOs, image review, rollback and the upstream lake backup
risk. See the full [operations runbook](../docs/OPERATIONS.md#m6-staging-checklist).
