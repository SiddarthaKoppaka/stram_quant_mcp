from __future__ import annotations

import json
from pathlib import Path

import pytest

from stram_quant_mcp import app, entrypoint
from stram_quant_mcp.audit import AuditEvent, AuditJournal
from stram_quant_mcp.audit import main as audit_main
from stram_quant_mcp.tools import export_schema


def test_app_validates_bind_settings(monkeypatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(app.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("STRAM_MCP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("STRAM_MCP_BIND_PORT", "8080")
    app.main()
    assert calls[0][1]["port"] == 8080

    monkeypatch.setenv("STRAM_MCP_BIND_HOST", "attacker.example")
    with pytest.raises(ValueError, match="BIND_HOST"):
        app.main()
    monkeypatch.setenv("STRAM_MCP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("STRAM_MCP_BIND_PORT", "0")
    with pytest.raises(ValueError, match="BIND_PORT"):
        app.main()


def test_schema_cli_checks_and_writes(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "schemas.json"
    monkeypatch.setattr(export_schema, "TARGET", target)
    monkeypatch.setattr("sys.argv", ["export-schema"])
    export_schema.main()
    assert json.loads(target.read_text())["tools"]
    monkeypatch.setattr("sys.argv", ["export-schema", "--check"])
    export_schema.main()
    target.write_text("stale")
    with pytest.raises(SystemExit, match="out of date"):
        export_schema.main()


def test_audit_verify_cli(monkeypatch, tmp_path: Path, capsys) -> None:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    db = tmp_path / "audit.db"
    journal = AuditJournal(db, b"k" * 32)
    journal.append(
        AuditEvent(
            request_id="request",
            tool="quant_list_series",
            authorization_outcome="allowed",
            policy_version="policy",
            duration_ms=1,
            outcome="success",
            image_digest="test",
        )
    )
    journal.close()
    monkeypatch.setattr(
        "sys.argv", ["audit", "verify", "--db", str(db), "--key-file", str(key_path)]
    )
    audit_main()
    assert json.loads(capsys.readouterr().out)["records"] == 1


def test_entrypoint_routes_commands(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(entrypoint, "serve", lambda: called.append("serve"))
    monkeypatch.setattr(entrypoint, "preflight", lambda: called.append("preflight") or 0)
    monkeypatch.setattr("sys.argv", ["entrypoint", "serve"])
    entrypoint.main()
    assert called == ["preflight", "serve"]
    called.clear()
    monkeypatch.setattr(entrypoint, "preflight", lambda: 0)
    monkeypatch.setattr("sys.argv", ["entrypoint", "preflight"])
    with pytest.raises(SystemExit) as result:
        entrypoint.main()
    assert result.value.code == 0
    monkeypatch.setattr(
        entrypoint,
        "data_plane_probe",
        lambda: _record_async(called, "data-plane-probe"),
    )
    monkeypatch.setattr("sys.argv", ["entrypoint", "data-plane-probe"])
    entrypoint.main()
    assert called == ["data-plane-probe"]
    monkeypatch.setattr("sys.argv", ["entrypoint", "unknown"])
    with pytest.raises(SystemExit, match="usage"):
        entrypoint.main()


async def _record_async(called: list[str], value: str) -> None:
    called.append(value)
