from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from stram_quant_mcp.audit import AuditEvent, AuditIntegrityError, AuditJournal, AuditWriteError


def _event() -> AuditEvent:
    return AuditEvent(
        request_id="request-1",
        principal_hash="a" * 64,
        issuer="https://issuer.example",
        tenant="stram",
        client_id="quant-client",
        tool="quant_get_series",
        schema_hash="b" * 64,
        authorization_outcome="allowed",
        required_scope="quant:read",
        data_scope={"series_keys": ["SPY"], "availability": "strict"},
        availability_mode="strict",
        policy_version="1.3.1",
        row_count=2,
        truncated=False,
        duration_ms=12.5,
        outcome="success",
        image_digest="sha256:test",
    )


def test_audit_chain_is_durable_minimal_and_verifiable(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    journal = AuditJournal(path, b"k" * 32, clock=lambda: datetime(2026, 10, 31, tzinfo=UTC))
    journal.append(_event())

    result = journal.verify()
    journal.close()

    assert result.records == 1
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as connection:
        stored = connection.execute("SELECT record_json FROM audit_records").fetchone()[0]
    record = json.loads(stored)
    assert record["previous_hash"] == "0" * 64
    assert record["principal_hash"] == "a" * 64
    assert "token" not in stored
    assert "result" not in stored


def test_tampering_or_wrong_key_fails_verification(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    journal = AuditJournal(path, b"k" * 32)
    journal.append(_event())
    journal.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE audit_records SET record_json = "
            "replace(record_json, 'SPY', 'QQQ') WHERE sequence=1"
        )

    with pytest.raises(AuditIntegrityError):
        AuditJournal(path, b"k" * 32)
    with pytest.raises(AuditIntegrityError):
        AuditJournal(path, b"z" * 32)


def test_writer_probe_commits_without_creating_an_audit_record_and_fails_closed(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    journal = AuditJournal(path, b"k" * 32)
    journal.probe_write()
    assert journal.verify().records == 0

    journal.close()
    with pytest.raises(AuditWriteError):
        journal.probe_write()
    assert journal.healthy is False
