"""Durable, hash-chained audit journal kept separate from application logs."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from stram_quant_mcp.settings import load_secret_file
from stram_quant_mcp.version import __version__

GENESIS_HASH = "0" * 64


class AuditIntegrityError(RuntimeError):
    """The persisted journal does not verify against its key and chain."""


class AuditWriteError(RuntimeError):
    """A terminal invocation outcome could not be made durable."""


class AuditEvent(BaseModel):
    """Minimum attributable facts for one terminal invocation outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str | None = Field(default=None, max_length=64)
    principal_hash: str | None = Field(default=None, max_length=64)
    issuer: str | None = Field(default=None, max_length=500)
    tenant: str | None = Field(default=None, max_length=100)
    client_id: str | None = Field(default=None, max_length=200)
    tool: str = Field(min_length=1, max_length=100)
    schema_hash: str | None = Field(default=None, max_length=64)
    authorization_outcome: str = Field(max_length=40)
    required_scope: str | None = Field(default=None, max_length=100)
    data_scope: dict[str, JsonValue] = Field(default_factory=dict)
    availability_mode: str | None = Field(default=None, max_length=20)
    policy_version: str = Field(max_length=100)
    row_count: int | None = Field(default=None, ge=0)
    truncated: bool | None = None
    duration_ms: float = Field(ge=0)
    outcome: str = Field(max_length=40)
    error_class: str | None = Field(default=None, max_length=80)
    image_digest: str = Field(max_length=200)


class AuditVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: int
    last_sequence: int
    last_hash: str
    key_id: str


class AuditJournal:
    """Single-writer SQLite journal with canonical HMAC-linked records."""

    def __init__(
        self,
        path: Path,
        key: bytes,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(key) < 32:
            raise ValueError("audit key must contain at least 32 bytes")
        if not path.parent.exists():
            raise ValueError("audit directory must already exist")
        self._path = path
        self._key = key
        self._key_id = hashlib.sha256(key).hexdigest()[:16]
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=2000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_records (
                sequence INTEGER PRIMARY KEY,
                record_json TEXT NOT NULL,
                record_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        if path.exists():
            path.chmod(0o600)
        self._healthy = True
        self.verify()

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def key_id(self) -> str:
        return self._key_id

    def pseudonymize(self, issuer: str, client_id: str, subject: str | None) -> str:
        principal = f"{issuer}|{client_id}|{subject or ''}".encode()
        return hmac.new(self._key, b"principal:" + principal, hashlib.sha256).hexdigest()

    def append(self, event: AuditEvent) -> str:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                last = self._connection.execute(
                    "SELECT sequence, record_hash FROM audit_records ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = int(last[0]) + 1 if last else 1
                previous_hash = str(last[1]) if last else GENESIS_HASH
                record: dict[str, JsonValue] = {
                    "sequence": sequence,
                    "event_time": self._clock().astimezone(UTC).isoformat(),
                    "previous_hash": previous_hash,
                    "key_id": self._key_id,
                    "service_version": __version__,
                    **event.model_dump(mode="json"),
                }
                canonical = _canonical(record)
                record_hash = hmac.new(self._key, canonical, hashlib.sha256).hexdigest()
                self._connection.execute(
                    "INSERT INTO audit_records(sequence, record_json, record_hash) "
                    "VALUES (?, ?, ?)",
                    (sequence, canonical.decode(), record_hash),
                )
                self._connection.execute("COMMIT")
                return record_hash
            except (sqlite3.Error, OSError, ValueError, TypeError) as error:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                self._healthy = False
                raise AuditWriteError("audit append failed") from error

    def verify(self) -> AuditVerification:
        with self._lock:
            previous_hash = GENESIS_HASH
            last_sequence = 0
            try:
                rows = self._connection.execute(
                    "SELECT sequence, record_json, record_hash FROM audit_records ORDER BY sequence"
                ).fetchall()
                for expected_sequence, (sequence, record_json, record_hash) in enumerate(rows, 1):
                    record = json.loads(record_json)
                    if (
                        sequence != expected_sequence
                        or record.get("sequence") != sequence
                        or record.get("previous_hash") != previous_hash
                        or record.get("key_id") != self._key_id
                    ):
                        raise AuditIntegrityError("audit sequence or chain is invalid")
                    expected_hash = hmac.new(
                        self._key, _canonical(record), hashlib.sha256
                    ).hexdigest()
                    if not hmac.compare_digest(str(record_hash), expected_hash):
                        raise AuditIntegrityError("audit record authentication failed")
                    previous_hash = str(record_hash)
                    last_sequence = int(sequence)
            except (sqlite3.Error, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._healthy = False
                if isinstance(error, AuditIntegrityError):
                    raise
                raise AuditIntegrityError("audit verification failed") from error
            self._healthy = True
            return AuditVerification(
                records=len(rows),
                last_sequence=last_sequence,
                last_hash=previous_hash,
                key_id=self._key_id,
            )

    def probe_write(self) -> None:
        """Commit an atomic insert/delete transaction to prove the journal is writable."""

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT INTO audit_records(sequence, record_json, record_hash) "
                    "VALUES (-1, '{}', 'probe')"
                )
                self._connection.execute("DELETE FROM audit_records WHERE sequence = -1")
                self._connection.execute("COMMIT")
                self._healthy = True
            except (sqlite3.Error, OSError, ValueError, TypeError) as error:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                self._healthy = False
                raise AuditWriteError("audit writer probe failed") from error

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--db", type=Path, required=True)
    verify.add_argument(
        "--key-file",
        type=Path,
        default=(
            Path(os.environ["STRAM_MCP_AUDIT_KEY_FILE"])
            if os.environ.get("STRAM_MCP_AUDIT_KEY_FILE")
            else None
        ),
    )
    args = parser.parse_args()
    if args.command == "verify":
        if not args.key_file:
            raise SystemExit("audit key file is required")
        journal = AuditJournal(args.db, load_secret_file(args.key_file))
        try:
            sys.stdout.write(journal.verify().model_dump_json() + "\n")
        finally:
            journal.close()


if __name__ == "__main__":
    main()
