"""Reject strong-copyleft runtime/development dependencies pending explicit review."""

from __future__ import annotations

import json
import re
import sys

DENIED = re.compile(r"(?<!L)GPL(?:-|\s|$)|AGPL|SSPL|GNU General Public License", re.IGNORECASE)
REVIEWED_PRIVATE_PACKAGES = {"stram-data"}


def main() -> None:
    packages = json.load(sys.stdin)
    if not isinstance(packages, list):
        raise SystemExit("invalid pip-licenses report")
    denied: list[str] = []
    unknown: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise SystemExit("invalid package record")
        name = str(package.get("Name", "unknown"))
        license_name = str(package.get("License", "")).strip()
        if (
            not license_name or license_name.upper() in {"UNKNOWN", "NONE"}
        ) and name not in REVIEWED_PRIVATE_PACKAGES:
            unknown.append(name)
        if DENIED.search(license_name) and "Lesser" not in license_name:
            denied.append(f"{name}: {license_name}")
    if denied or unknown:
        details = "; ".join((*denied, *(f"{name}: unknown" for name in unknown)))
        raise SystemExit(f"unreviewed dependency licenses: {details}")
    sys.stdout.write(f"license scan: {len(packages)} packages reviewed\n")


if __name__ == "__main__":
    main()
