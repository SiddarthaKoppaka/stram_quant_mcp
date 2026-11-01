# Reviewed security exceptions

Exceptions block release unless every field below remains current. They are not blanket suppressions.

## PYSEC-2026-113 / CVE-2026-25087

- **Dependency:** `pyarrow==22.0.0`, transitively constrained to `<23` by frozen `stram-data==1.3.1`
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
- **Mitigations:** no arbitrary file/upload/URL tools; closed schemas; worker process isolation and hard
  memory/deadline limits; read-only curated lake; process-local socket denial; container bounds and
  host egress allowlists.
- **Resolution:** test and adopt the first approved frozen Data Plane release whose declared PyArrow
  range includes 23.0.1+, then remove the `pip-audit --ignore-vuln PYSEC-2026-113` gate argument.

Overriding `stram-data`'s declared `pyarrow<23` range in this service was rejected because an untested
dependency override creates a larger data-correctness and compatibility risk than this unreachable
Python-binding advisory.
