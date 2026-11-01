# Operations

This runbook covers the V1 single-replica service. Local and synthetic-lake behavior is verified. A
read-only SIDDU inventory was completed on 2026-10-31; no service, file, firewall or identity was
changed. Deployment, the non-production identity provider, private TLS route, telemetry destination,
audit retention and rollback are **not yet verified** and require an authorized M6 change.

## Build and release gates

Use the frozen lock and exact `stram-data` 1.3.1 pin:

```bash
uv sync --frozen --dev
uv run pytest --cov --cov-report=term-missing
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run python -m stram_quant_mcp.tools.export_schema --check
uv run python tools/check_docs.py
uv run python tools/verify_otel_fixture.py
```

The complete release pipeline also runs dependency, secret and license gates; creates a reproducible
CycloneDX SBOM; builds with provenance; inspects the non-root image; runs container preflight with a
read-only synthetic lake; and scans the image. CI is authoritative for the image checks. Do not build
a release image from an unreviewed lockfile or mutable base tag.

Container builds need the pinned upstream wheel prepared outside the final image context:

```bash
tools/prepare_container_dependency.sh ../stram_data
docker buildx build --pull=false --load --provenance=mode=max --sbom=true \
  --metadata-file build/build-metadata.json --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -t stram-quant-mcp:review .
```

Expected: image user `10001:10001`; pinned Python 3.12.12 base digest; no Git/build tools in runtime;
and only the approved runtime dependency set. Local Docker evidence does not qualify Podman on SIDDU.

## Configuration

Secrets are file-mounted, never passed on the command line or stored in service environment files.
Required service variables are:

| Variable | Purpose |
|---|---|
| `STRAM_MCP_PARQUET_ROOT` | Explicit lake root, `/lake` in the container |
| `STRAM_MCP_AUDIT_DB` | SQLite audit journal, on the writable audit volume |
| `STRAM_MCP_AUDIT_KEY_FILE` | Separate read-only HMAC key file, minimum 32 bytes |
| `STRAM_MCP_ISSUER_URL` | Exact HTTPS token issuer |
| `STRAM_MCP_RESOURCE_URL` | Exact HTTPS MCP audience/resource |
| `STRAM_MCP_JWKS_URL` | Exact HTTPS JWKS endpoint |
| `STRAM_MCP_TENANT_ID` | Single admitted tenant |
| `STRAM_MCP_ALLOWED_HOSTS` | Comma-separated Host allowlist |

Optional production controls include allowed browser origins/algorithms, image digest, OTLP endpoint,
worker restart cap/backoff/shutdown budget, bind host and port. Defaults are three consecutive worker
replacements, 0.25-second replacement delay and five-second shutdown drain. Security and correctness
defaults require review before change; update policy/release evidence with any change.

Use [service.env.example](../deploy/service.env.example) and
[runtime.env.example](../deploy/runtime.env.example) as field inventories, not ready-to-run files.
They contain no bearer token or audit key.

## Startup and health

The container `serve` command runs preflight before opening the listener. It fails unless:

- effective UID is non-root;
- the container root and lake are distinct read-only mounts;
- the exact `stram-data` and Data Interface versions match;
- the real public interface can describe availability policies.

Application lifespan then verifies the audit chain, starts the worker, checks dependency versions and
policy hash, and warms JWKS. Only then is readiness true.

```bash
curl --fail --silent http://127.0.0.1:8800/health/live
curl --fail --silent http://127.0.0.1:8800/health/ready
```

`/health/live` proves only that the HTTP process responds. `/health/ready` returns only `ready` or
`not_ready`; the private proxy must route MCP traffic only while ready. Neither endpoint requires a
token or exposes dependency details.

## Rootless Podman and Quadlet

### Read-only inventory

Verified on SIDDU on 2026-10-31 without mutation: Podman 6.1.4 is rootless with Netavark and cgroup v2;
existing application containers are generated from user Quadlets; published application ports are
loopback-only; Tailscale is active; no Tailscale Serve route or Caddy, Nginx or Traefik service is
configured; and `nft` and `firewall-cmd` are installed. The canonical lake is on a read-write ext4
filesystem and its parent is private to the existing Data Plane identity. There is no dedicated MCP
identity, kernel-enforced read-only lake view, private TLS route or MCP network policy yet.

### Required identity and mount design

An administrator must create a dedicated, non-login host identity used only for rootless Podman. It
must not join the Data Plane, Docker, Kafka, PostgreSQL or ingestion groups. Its writable ownership is
limited to its rootless container storage and the MCP audit directory.

Do not grant that identity traversal through the private canonical-lake parent. Instead, create a
root-owned system mount whose source is the Parquet directory and whose separate target is exposed to
the MCP identity. The mount must be `bind,ro,nosuid,nodev,noexec`; the container binds only that target
to `/lake` with `ro`. This preserves the canonical parent permissions and adds both host- and
container-kernel read-only enforcement. Before admission, prove an unprivileged host write and an
in-container write both fail, while `preflight` can describe availability policies. Do not use ACLs,
supplementary Data Plane groups, `chmod`, `chown`, `:U` or world-writable permissions on the canonical
lake.

Run container UID/GID `10001:10001` with a reviewed rootless user-namespace mapping that maps the host
service identity only to that container identity. Prove the mapping with `podman unshare cat
/proc/self/uid_map` and a disposable audit-file append before installing or enabling the Quadlet.

The reviewed deployment unit must enforce all of these controls:

- image by registry digest, never tag alone;
- container UID/GID 10001, read-only root and lake bind;
- only a bounded `/tmp` tmpfs (`rw,noexec,nosuid,nodev,size=64m`) and audit storage writable;
- all capabilities dropped, no-new-privileges, bounded memory/CPU/PIDs and stop timeout;
- loopback-only published port behind the private TLS proxy;
- private network plus host firewall egress allowlist for only JWKS/issuer and optional OTLP;
- service environment file without secrets, audit key as a read-only secret/file mount;
- restart delay and bounded systemd start rate; no endless crash loop.

Podman's network name is not an egress policy. Apply an administrator-owned host egress policy to the
dedicated service identity/network namespace: deny by default and allow only the exact resolved
issuer/JWKS HTTPS destinations plus an approved OTLP destination. Image pulls happen outside the
running service policy. Record the ruleset and prove denied DNS, IPv4 and IPv6 destinations from the
container; do not treat a successful JWKS request as evidence that other egress is blocked.

Rootless user namespaces also change host ownership mapping. An operator must prove the mounted lake
is unreadable for writes and the audit volume is writable by the mapped container UID before enabling
the unit. Do not solve mapping failures with world-writable permissions or ownership changes to the
Data Plane.

The query worker has an additional kernel boundary. Build the exact image, then run:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  stram-quant-mcp:review worker-network-probe
```

Expected: `{"status":"worker_network_isolated"}`. The command installs the production seccomp filter
and directly invokes libc `socket` and `io_uring_setup`; both must return kernel `EPERM`. During a
staging call, also record `Seccomp: 2` and a nonzero `Seccomp_filters` for the query-worker PID from
`/proc/PID/status`. These checks prove the worker boundary; separate egress tests prove the container
HTTP-process policy.

Then mount the synthetic lake read-only with container networking disabled and run
`worker-interface-probe`. Expected: `{"status":"worker_interface_ok"}`. This starts the same spawned
worker used by the server after its seccomp filter is installed and calls the pinned public Data
Interface; it detects a filter that breaks legitimate DuckDB/Parquet work.

The older Docker user-systemd template is retained for reference under `deploy/systemd/`; it is not
the approved SIDDU runtime. See [Deployment](../deploy/README.md) for the staging checklist.

## Staging identity and HTTP policy

The IdP owner must supply a non-production client and record these exact values in restricted staging
configuration and release evidence:

- issuer: the token `iss` value and `STRAM_MCP_ISSUER_URL`, byte-for-byte equivalent after configured
  URL parsing;
- resource: one private HTTPS MCP URL used as both JWT `aud` and `STRAM_MCP_RESOURCE_URL`;
- JWKS: one HTTPS URL reachable under the egress allowlist, with only reviewed asymmetric keys;
- tenant: one exact claim name (default `tenant_id`) and admitted value;
- algorithm: `RS256` unless a separate review selects `RS384`, `RS512`, `ES256` or `ES384`;
- scopes: `quant:discover`, `quant:read`, `quant:lineage` and `quant:analytics`, granted independently
  and only as required by the test identity.

Tokens must contain `exp`, `iat`, `iss`, `aud`, `sub`, `client_id` or `azp`, tenant and `scope` or
`scp`. The verifier allows 30 seconds of clock leeway. Missing, forged, expired, wrong-audience,
wrong-tenant and insufficient-scope tokens must be exercised through the staging HTTP endpoint before
any real-lake call.

The private TLS proxy publishes no public listener and forwards only to the loopback container port.
Set `STRAM_MCP_ALLOWED_HOSTS` to the exact client-facing private hostname, including a port only if the
client sends one. Leave origins empty for the official non-browser SDK; if a browser consumer is later
approved, list only its exact HTTPS origins. Uvicorn ignores forwarded headers, so the application
does not trust proxy-supplied client identity. Record certificate ownership, Tailscale ACL, proxy body
limit and both rejected Host and Origin evidence before routing traffic.

## Audit verification and checkpoints

The journal and HMAC key use different mounts and access controls. The service may append the journal
and read the key. Application users may read neither. Operator/security readers use protected backup
copies, not the live file.

1. Stop admission or make a consistent SQLite online backup. Copying only the main file while WAL is
   active is not a backup.
2. Verify the copy in a protected operator context:

   ```bash
   uv run python -m stram_quant_mcp.audit verify \
     --db /protected/copy/audit.sqlite3 --key-file /protected/secrets/audit-key
   ```

3. Record `last_sequence`, `last_hash`, `key_id`, image digest, policy version, database identity and
   UTC time in a host-owned signed checkpoint outside service-writable storage.
4. Alert on verification failure, sequence rollback, unexpected key ID or checkpoint mismatch. Preserve
   the original journal; never truncate or repair it in place.

Proposed retention is 90 days online and one year encrypted/archive, pending owner approval. HMAC
chaining detects modification with the key; external signed checkpoints are required to detect a
host-root attacker rewriting both journal and chain.

## Audit-key rotation

Rotation starts a new explicit chain:

1. Quiesce and stop the service; verify and checkpoint the old journal.
2. Create a new approved random key and empty audit database under the secret/volume mechanisms. Never
   pass the key as an argument or bake it into an image.
3. Record old terminal hash/key ID and new key ID in the signed rotation record.
4. Update key and database references atomically, start, require readiness and produce the first new
   checkpoint.
5. Retain the old journal and key under the archive policy for the same retention period.

Rollback across rotation restores image, configuration, journal and matching key as one unit.

## Worker recovery and shutdown

One worker executes one Data Interface call at a time. Queue wait, per-tool deadline, response bytes,
memory, restart cap/backoff and shutdown drain are bounded. A timeout or crash kills the old worker.
After the configured number of consecutive replacements, the restart circuit opens and readiness
becomes false. One valid worker response resets the consecutive counter.

On SIGTERM, the server first becomes unready, stops normal listener admission, drains the worker up to
the configured budget, then terminates/kills it, closes audit and flushes telemetry best effort.
Cancellation and forced shutdown cannot leave a replacement worker behind. Data queries and audit
appends are never automatically retried.

For a deadline or crash, verify the terminal audit outcome and whether the replacement is ready. For
repeated failures, remove the instance from rotation and preserve image/config version, restart count,
request/trace IDs and audit checkpoint. Do not collect raw arguments, results or tokens.

## Telemetry and alerting

OTLP/HTTP is optional. When configured, only HTTPS is accepted outside explicit loopback testing.
Batch queues and export deadlines are bounded. Export failure degrades observability but not safe
reads; audit failure blocks result release and readiness.

Expected signals:

- structured JSON stdout events correlated by request/trace/span IDs;
- HTTP request count/duration by method and status;
- tool count/duration by fixed tool, outcome and availability mode;
- readiness state;
- worker deadline, unavailable and overload outcomes in safe events/audit.

No metric label or span/log field contains caller, tenant, subject, series, indicator, raw error,
argument, result, token or filesystem path. Run `uv run python tools/verify_otel_fixture.py` after any
telemetry change. Production dashboards, alert destinations, sampling and SLOs remain M6 decisions.

## Backup, restore and rollback

Back up configuration references, the SQLite journal through online backup, signed checkpoints and
audit keys through the approved secret backup process. The lake is owned and recovered by
`stram_data`; this service never writes or repairs it. Current lack of an approved Data Plane lake
backup is an external release risk.

Restore into isolation, apply least privilege, verify the complete audit chain against its matching
key/checkpoint, then run container preflight and readiness. Do not route traffic during a restore
drill.

Rollback by previous approved image digest plus its matching configuration/policy version. Leave lake
and audit journal untouched unless the release rotated audit keys, in which case restore the complete
rotation unit. After rollback: run preflight, verify the chain, require readiness and call one bounded
discovery tool with a non-production credential. Record exact digests and outcomes.

## Incident response

1. Stop private-proxy routing without deleting containers, volumes, logs or keys.
2. Record UTC time, image digest, policy version, readiness, worker restart count, audit terminal hash
   and safe request/trace IDs.
3. Classify authentication abuse, authorization abuse, audit integrity, data correctness, resource
   exhaustion, dependency outage or suspected host compromise.
4. Preserve consistent audit copies/checkpoints and restricted proxy/host/collector evidence.
5. Recover through controlled restart, matching-image rollback or isolated restore. Escalate data
   correctness to `stram_data`; never patch the lake here.
6. Re-enable only after verification, readiness and an approved incident decision.

## PyArrow security exception

The frozen Data Plane requires `pyarrow==22.0.0`, affected by CVE-2026-25087 in an Arrow IPC C++ path
not exposed through the Python bindings used here. The service accepts no files, paths, IPC payloads,
URLs or arbitrary queries and reads only a curated read-only Parquet lake in an isolated worker. This
reviewed high-severity exception expires 2027-01-31 or when an approved `stram-data` release supports
PyArrow 23.0.1 or later. Full owner, evidence and removal trigger are in
[Security exceptions](SECURITY_EXCEPTIONS.md). It remains a release exception, not a general waiver.

## M6 staging checklist

All items below are **not yet verified** until an authorized SIDDU change records evidence.

- [ ] Approved immutable image digest, SBOM, provenance, vulnerability report and rollback digest.
- [ ] Dedicated rootless Podman host identity with no Data Plane write privilege.
- [ ] Root-owned `bind,ro,nosuid,nodev,noexec` lake view without changing canonical permissions;
  unprivileged host and in-container writes fail, while preflight succeeds.
- [ ] Podman-managed audit storage, separate HMAC secret, online-backup path, signed checkpoint owner and
  approved retention.
- [ ] Native worker probe blocks libc socket and `io_uring`; query-worker `/proc` status shows seccomp
  filter mode during a real call.
- [ ] Private network, loopback publication, TLS proxy Host/Origin policy and host-enforced egress only
  to exact JWKS/issuer and approved OTLP endpoints.
- [ ] Non-production issuer/client with exact audience, tenant claim, asymmetric algorithm and least
  tool scopes; missing, forged, expired, wrong-audience, wrong-tenant and insufficient-scope checks.
- [ ] Liveness/readiness routing and alerts; JWKS, worker, audit and policy/version startup failures.
- [ ] Authenticated official MCP client calls all representative tool classes against the mounted lake;
  quantitative records are genuine and audit outcomes verify.
- [ ] Bounded overload, deadline, worker crash, restart-circuit and graceful-shutdown exercise.
- [ ] Telemetry redaction verification and operator dashboard/alert/SLO ownership.
- [ ] Audit online backup, independent chain verification, external checkpoint and restore drill.
- [ ] Previous-digest rollback restores readiness without lake mutation or chain damage.
- [ ] Exact commands, timestamps, safe outcomes and remaining risks added to `PLANS.md`.

Do not enable staging traffic until every blocking item has an owner and evidence.
