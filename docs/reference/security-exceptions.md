# Reviewed security exceptions

Exceptions block release unless every field below remains current. They are not blanket suppressions.

## PYSEC-2026-113 / CVE-2026-25087

- **Dependency:** `pyarrow==22.0.0`, transitively constrained to `<23` by pinned `stram-data==1.9.0`
- **Severity:** high (CVSS 7.0)
- **Owner:** Stram Quant MCP security/release owner
- **Review date:** 2026-10-31
- **Expiry:** 2027-01-31, or immediately when a tested `stram-data` release permits PyArrow 23.0.1+
- **Source:** [GitHub-reviewed advisory GHSA-rgxp-2hwp-jwgg](https://github.com/advisories/GHSA-rgxp-2hwp-jwgg)
- **Affected surface:** a C++ Arrow IPC file reader with `PreBufferMetadata` enabled and variadic buffers.
  The advisory states that this functionality is not exposed by Python bindings.
- **Applicability:** not reachable in this service. The MCP accepts no files, paths, IPC payloads, or
  arbitrary queries. The pinned public Data Interface reads a host-curated, read-only Parquet lake;
  this repository contains no Arrow IPC reader or pre-buffering call.
- **Mitigations:** no arbitrary file/upload/URL tools; closed schemas; no lake mount or query path in this service (it only calls the Data Plane over HTTP); bounded
  concurrency and deadlines; container bounds and host egress allowlists.
- **Resolution:** test and adopt the first approved frozen Data Plane release whose declared PyArrow
  range includes 23.0.1+, then remove the `pip-audit --ignore-vuln PYSEC-2026-113` gate argument.

Overriding `stram-data`'s declared `pyarrow<23` range in this service was rejected because an untested
dependency override creates a larger data-correctness and compatibility risk than this unreachable
Python-binding advisory.

## CVE-2026-102554 / GHSA-xxph-c9ww-hj94

- **Dependency:** `com.google.guava:guava==33.6.0-jre`, bundled by the optimized Keycloak 26.8.1
  staging image
- **Severity:** high in the pinned Trivy 0.68.1 scan (the upstream Guava advisory rates it moderate)
- **Owner:** Stram Quant MCP staging security/release owner
- **Review date:** 2026-11-01
- **Expiry:** 2026-12-01, or immediately when an approved Keycloak image includes Guava 33.7.2+
- **Source:** [Guava advisory GHSA-xxph-c9ww-hj94](https://github.com/google/guava/security/advisories/GHSA-xxph-c9ww-hj94)
- **Affected surface:** denial of service during Java native deserialization of crafted
  `CompactHashMap`, `CompactHashSet`, or `MapMakerInternalMap` objects.
- **Applicability:** no Java native-serialization endpoint is exposed by the reviewed staging
  topology. Only Keycloak OIDC realm paths are published over tailnet-only HTTPS; the admin and
  management endpoints remain loopback-only. This is a constrained residual-risk assessment, not
  a claim that the transitive library is absent or independently approved.
- **Mitigations:** private Tailscale access; no Funnel or public listener; confidential service
  clients; bounded Keycloak memory, PIDs, request queue and systemd restart rate; management and
  admin paths excluded from the proxy.
- **Resolution:** rebuild and rescan from the first supported Keycloak 26 image that contains Guava
  33.7.2 or later, re-run live OIDC/MCP acceptance, then remove this exception.
