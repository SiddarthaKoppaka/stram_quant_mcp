# SIDDU deployment preparation

These files are review references only. M6 staging and every SIDDU change require separate
authorization. No production secret or private key belongs in this repository; deployed host paths
and image digests belong in restricted release evidence, not these examples.

The target runtime is a dedicated rootless Podman service identity managed through user-systemd
Quadlet. The existing [Docker systemd unit](systemd/stram-quant-mcp.service) is retained as an older
control reference; it is not approved for SIDDU and must not be installed unchanged.

Before staging, an operator must provide and review:

- immutable image and rollback digests with retained SBOM, provenance and vulnerability evidence;
- a dedicated non-login rootless Podman identity with no lake or ingestion write privilege;
- a Data Plane-owned lake view/snapshot that is host- and container-enforced read-only;
- Podman-owned durable audit storage, a separately mounted HMAC key, online-backup access and
  host-owned signed checkpoint/archive storage;
- loopback-only publication behind the private TLS proxy, exact Host/Origin allowlists and a host
  firewall allowing egress only to exact issuer/JWKS and optional OTLP destinations;
- a non-production OAuth client with exact resource/audience, tenant claim and least tool scopes;
- health routing, restart/start-rate limits, resource limits, alert/SLO owners, retention approval and
  rollback authority.

Use [runtime.env.example](runtime.env.example) for host/runtime values and
[service.env.example](service.env.example) for non-secret application values. Store deployed copies
outside the repository under the dedicated identity with restrictive access. The service environment
must not contain bearer tokens or the audit key. Rootless UID mapping must be proven; never use
world-writable permissions or change Data Plane ownership to satisfy a container mount.

The final Quadlet must pin the image by digest; run as container UID/GID 10001; set read-only root and
lake; drop all capabilities; enable no-new-privileges; bound CPU, memory, PIDs, tmpfs and stop time;
mount only audit state writable; and publish only to loopback. Podman networking is not an egress
allowlist, so host firewall evidence is mandatory.

The 2026-10-31 read-only inventory confirmed the host supports rootless Podman/Quadlet, Netavark,
cgroup v2, Tailscale and administrator firewall tooling. It also confirmed that no dedicated MCP
identity, read-only lake view, TLS route or MCP firewall policy exists. Do not adapt an existing
application Quadlet or run under the Data Plane identity. The reviewed identity, mount, network and
audit design and exact validation commands are in
[Operations](../docs/OPERATIONS.md#rootless-podman-and-quadlet).

Follow the complete [M6 staging checklist](../docs/OPERATIONS.md#m6-staging-checklist), audit procedure,
health checks and rollback runbook. Do not enable the unit or route traffic until every prerequisite is
authorized and evidenced.
