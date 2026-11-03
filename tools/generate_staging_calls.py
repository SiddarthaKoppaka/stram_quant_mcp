"""Generate bounded live acceptance calls through the public Stock Data Interface."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from stram_data.data_interface import DataInterface, DerivedQuery


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    interface = DataInterface(parquet_root=args.parquet_root)
    catalog = interface.get_catalog(limit=1)
    if not catalog:
        raise SystemExit("BLOCKED: Stock lake catalog is empty")
    series = catalog[0]
    indicators = interface.list_indicators()
    if not indicators:
        raise SystemExit("BLOCKED: Stock lake has no derived indicators")
    indicator = indicators[0]
    subjects = interface.list_indicator_subjects(indicator.indicator_id)
    derived = interface.get_derived(
        DerivedQuery(
            indicator_id=indicator.indicator_id,
            subject=subjects[0] if subjects else None,
            start_date=indicator.first_observation,
            end_date=indicator.first_observation,
            limit=1,
        )
    )
    if not derived.rows:
        raise SystemExit("BLOCKED: no derived event is available for lineage acceptance")
    payload = {
        "calls": [
            {"class": "discovery", "tool": "quant_list_series", "arguments": {"limit": 1}},
            {"class": "read", "tool": "quant_get_freshness", "arguments": {}},
            {
                "class": "lineage",
                "tool": "quant_get_lineage",
                "arguments": {"event_id": str(derived.rows[0].event_id)},
            },
            {
                "class": "analytics",
                "tool": "quant_get_aligned_panel",
                "arguments": {
                    "columns": [{"name": "sample", "series_key": series.series_key}],
                    "start_date": str(series.first_observation),
                    "end_date": str(series.first_observation),
                    "availability": "strict",
                    "limit": 5,
                },
            },
        ]
    }
    output = Path(args.output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent, prefix="staging-calls.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("PASS: generated four bounded acceptance calls from genuine Stock lake metadata")


if __name__ == "__main__":
    main()
