"""Enforce the image CVE policy with only the documented PyArrow exception."""

from __future__ import annotations

import json
import sys

REVIEWED_EXCEPTIONS = {("pyarrow", "CVE-2026-25087")}
BLOCKING_SEVERITIES = {"HIGH", "CRITICAL"}


def main() -> None:
    report = json.load(sys.stdin)
    results = report.get("Results", [])
    if not isinstance(results, list):
        raise SystemExit("invalid Trivy report")
    blocked: list[str] = []
    ignored = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        vulnerabilities = result.get("Vulnerabilities") or []
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                continue
            severity = str(vulnerability.get("Severity", "")).upper()
            if severity not in BLOCKING_SEVERITIES:
                continue
            key = (
                str(vulnerability.get("PkgName", "")).lower(),
                str(vulnerability.get("VulnerabilityID", "")),
            )
            if key in REVIEWED_EXCEPTIONS:
                ignored += 1
            else:
                blocked.append(f"{key[0]}:{key[1]}:{severity}")
    if blocked:
        raise SystemExit("unreviewed high/critical image findings: " + ", ".join(blocked))
    sys.stdout.write(f"image scan: clean with {ignored} reviewed exception(s)\n")


if __name__ == "__main__":
    main()
