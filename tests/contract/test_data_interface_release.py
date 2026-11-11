from __future__ import annotations

import ast
import inspect
import json
import tomllib
from pathlib import Path

import stram_data
import stram_data.data_interface as public

ROOT = Path(__file__).parents[2]
SNAPSHOT = json.loads((ROOT / "contracts/data_interface.realtime-batch.json").read_text())


def test_exact_data_interface_release_is_installed() -> None:
    assert stram_data.__version__ == SNAPSHOT["package_version"] == "1.9.0"
    assert public.INTERFACE_VERSION == SNAPSHOT["interface_version"] == "1"


def test_public_surface_contains_the_frozen_release_contract() -> None:
    assert set(SNAPSHOT["required_public_names"]) <= set(public.__all__)
    methods = {
        name
        for name, member in inspect.getmembers(public.DataInterface, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == set(SNAPSHOT["public_methods"])


def test_dependency_is_pinned_to_the_release_commit() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    dependency = next(item for item in project["dependencies"] if item.startswith("stram-data "))
    assert dependency.endswith(f"@{SNAPSHOT['package_commit']}")


def test_source_imports_only_the_public_data_interface() -> None:
    violations: list[str] = []
    for path in (ROOT / "src/stram_quant_mcp").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.startswith("stram_data") and module not in {
                    "stram_data",
                    "stram_data.data_interface",
                }:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
    assert violations == []
