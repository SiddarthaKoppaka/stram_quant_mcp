from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src/stram_quant_mcp"
FORBIDDEN_CALLS = {"eval", "exec", "compile", "system", "popen"}
FORBIDDEN_IMPORTS = {"duckdb", "subprocess"}


def test_tool_plane_contains_no_sql_or_arbitrary_execution() -> None:
    violations: list[str] = []
    for path in SOURCE.rglob("*.py"):
        source = path.read_text()
        if path.name != "audit.py" and ("SELECT " in source.upper() or "INSERT " in source.upper()):
            violations.append(f"{path.relative_to(ROOT)} contains SQL")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = {alias.name.split(".")[0] for alias in node.names}
                if modules & FORBIDDEN_IMPORTS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} forbidden import")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in FORBIDDEN_IMPORTS
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} forbidden import")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} forbidden call")
                if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} forbidden call")
    assert violations == []


def test_audit_sql_is_static_and_never_caller_constructed() -> None:
    path = SOURCE / "audit.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    sql_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]
    assert sql_calls
    assert all(
        call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        for call in sql_calls
    )
