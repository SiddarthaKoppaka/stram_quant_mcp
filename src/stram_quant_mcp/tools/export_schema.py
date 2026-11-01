"""Generate or verify the reviewed MCP tool-definition artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stram_quant_mcp.tool_registry import exported_definitions

ROOT = Path(__file__).parents[3]
TARGET = ROOT / "contracts/tool_schemas.json"


def render() -> str:
    return json.dumps(exported_definitions(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != expected:
            raise SystemExit("tool schema artifact is out of date")
        return
    TARGET.write_text(expected)


if __name__ == "__main__":
    main()
