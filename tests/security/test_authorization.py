from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken

from stram_quant_mcp.audit import AuditJournal
from stram_quant_mcp.errors import ToolForbiddenError, ToolOverloadedError
from stram_quant_mcp.policy import InvocationService, RateLimiter
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.worker import QueryWorker


class CancellableWorker:
    policy_version = "test-policy"

    async def call(self, *_args: object) -> dict[str, object]:
        await asyncio.Event().wait()
        return {}


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        parquet_root=Path(__file__).parents[1] / "fixtures/lake",
        audit_db_path=tmp_path / "audit.sqlite3",
        audit_key_path=tmp_path / "audit.key",
        issuer_url="http://testserver/issuer",
        resource_url="http://testserver/mcp",
        jwks_url="http://testserver/jwks",
        tenant_id="stram",
        allowed_hosts=("testserver",),
        allow_insecure_http=True,
        image_digest="sha256:test",
    )


def _token(*, scopes: list[str], tenant: str = "stram") -> AccessToken:
    return AccessToken(
        token="verified",  # noqa: S106
        client_id="quant-client",
        scopes=scopes,
        expires_at=4_102_444_800,
        resource="http://testserver/mcp",
        subject="researcher-1",
        claims={"iss": "http://testserver/issuer", "tenant_id": tenant},
    )


def _records(path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT record_json FROM audit_records ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_rate_limiter_bounds_principals_and_evicts_idle_entries() -> None:
    now = 100.0
    limiter = RateLimiter(1, 1, clock=lambda: now)
    assert limiter.allow("first")
    assert not limiter.allow("first")
    assert not limiter.allow("second")

    now = 161.0
    assert limiter.allow("second")


@pytest.mark.asyncio
async def test_scope_and_tenant_are_enforced_before_dispatch_and_audited(tmp_path) -> None:
    settings = _settings(tmp_path)
    audit = AuditJournal(settings.audit_db_path, b"k" * 32)
    worker = QueryWorker(settings.parquet_root)
    service = InvocationService(settings, worker, audit, RateLimiter(30, 10))

    with pytest.raises(ToolForbiddenError):
        await service.invoke("quant_list_series", {}, "wrong-scope", _token(scopes=["quant:read"]))
    with pytest.raises(ToolForbiddenError):
        await service.invoke(
            "quant_list_series",
            {},
            "wrong-tenant",
            _token(scopes=["quant:discover"], tenant="other"),
        )

    records = _records(settings.audit_db_path)
    audit.close()
    assert [record["outcome"] for record in records] == [
        "authorization_denied",
        "authorization_denied",
    ]
    assert all(record["authorization_outcome"] == "denied" for record in records)


@pytest.mark.asyncio
async def test_success_and_rate_limit_each_create_one_terminal_record(tmp_path) -> None:
    settings = _settings(tmp_path)
    audit = AuditJournal(settings.audit_db_path, b"k" * 32)
    worker = QueryWorker(settings.parquet_root)
    await worker.start()
    service = InvocationService(settings, worker, audit, RateLimiter(1, 10))
    token = _token(scopes=["quant:discover"])
    try:
        result = await service.invoke("quant_list_series", {"limit": 1}, "success", token)
        with pytest.raises(ToolOverloadedError):
            await service.invoke("quant_list_series", {"limit": 1}, "limited", token)
    finally:
        await worker.close()

    records = _records(settings.audit_db_path)
    audit.close()
    assert len(result["data"]["entries"]) == 1
    assert [record["outcome"] for record in records] == ["success", "rate_limited"]
    assert records[0]["row_count"] == 1
    assert records[0]["data_scope"] == {}


@pytest.mark.asyncio
async def test_cancelled_invocation_creates_one_terminal_record(tmp_path) -> None:
    settings = _settings(tmp_path)
    audit = AuditJournal(settings.audit_db_path, b"k" * 32)
    service = InvocationService(  # type: ignore[arg-type] - deterministic cancellation boundary
        settings, CancellableWorker(), audit, RateLimiter(30, 10)
    )
    invocation = asyncio.create_task(
        service.invoke(
            "quant_list_series",
            {"limit": 1},
            "cancelled",
            _token(scopes=["quant:discover"]),
        )
    )
    await asyncio.sleep(0)
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    records = _records(settings.audit_db_path)
    audit.close()
    assert [record["outcome"] for record in records] == ["cancelled"]
    assert records[0]["error_class"] == "CancelledError"
