# Changelog

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
