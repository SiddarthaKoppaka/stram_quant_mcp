"""Fail CI when detect-secrets reports any unreviewed candidate."""

from __future__ import annotations

import json
import sys


def main() -> None:
    report = json.load(sys.stdin)
    results = report.get("results")
    if not isinstance(results, dict):
        raise SystemExit("invalid detect-secrets report")
    candidates = sum(len(items) for items in results.values() if isinstance(items, list))
    if candidates:
        raise SystemExit(f"detect-secrets found {candidates} unreviewed candidate(s)")
    sys.stdout.write("secret scan: no candidates\n")


if __name__ == "__main__":
    main()
