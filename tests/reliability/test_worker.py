from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from stram_quant_mcp.errors import (
    ToolOverloadedError,
    ToolResourceLimitError,
    ToolTimeoutError,
    ToolUnavailableError,
)
from stram_quant_mcp.worker import QueryWorker
from tests.support.worker_target import controlled_worker, network_probe_worker

ROOT = Path(__file__).parents[2]
LAKE = ROOT / "tests/fixtures/lake"


@pytest.mark.asyncio
async def test_real_worker_calls_the_pinned_interface_and_shuts_down() -> None:
    worker = QueryWorker(LAKE)
    await worker.start()
    result = await worker.call(
        "quant_get_series",
        {
            "series_keys": ["SPY"],
            "kinds": ["market_bar"],
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
        },
        "real",
        10.0,
    )
    data = result["data"]
    assert isinstance(data, dict) and data["count"] == 2
    pid = worker.pid
    assert pid is not None and worker.ready
    assert worker.dependency_versions_match
    assert worker.policy_version != "unknown"
    await worker.close()
    assert not worker.ready


@pytest.mark.asyncio
async def test_timeout_kills_and_replaces_the_worker() -> None:
    worker = QueryWorker(LAKE, process_target=controlled_worker)
    await worker.start()
    original = worker.pid
    with pytest.raises(ToolTimeoutError):
        await worker.call("probe", {"delay": 0.2}, "timeout", 0.02)
    assert worker.pid is not None and worker.pid != original
    assert worker.restart_count == 1
    recovered = await worker.call("probe", {}, "recovered", 1.0)
    assert recovered["request_id"] == "recovered"
    await worker.close()


@pytest.mark.asyncio
async def test_crash_isolated_and_next_call_recovers() -> None:
    worker = QueryWorker(LAKE, process_target=controlled_worker)
    await worker.start()
    original = worker.pid
    with pytest.raises(ToolUnavailableError):
        await worker.call("probe", {"crash": True}, "crash", 1.0)
    assert worker.pid is not None and worker.pid != original
    assert worker.restart_count == 1
    assert (await worker.call("probe", {}, "after", 1.0))["request_id"] == "after"
    await worker.close()


@pytest.mark.asyncio
async def test_backpressure_rejects_instead_of_queueing_without_bound() -> None:
    worker = QueryWorker(
        LAKE,
        process_target=controlled_worker,
        max_queued=0,
        queue_wait_seconds=0.02,
    )
    await worker.start()
    active = asyncio.create_task(worker.call("probe", {"delay": 0.1}, "active", 1.0))
    await asyncio.sleep(0)
    with pytest.raises(ToolOverloadedError):
        await worker.call("probe", {}, "rejected", 1.0)
    assert (await active)["request_id"] == "active"
    await worker.close()


@pytest.mark.asyncio
async def test_cancelled_call_leaves_no_orphan_query() -> None:
    worker = QueryWorker(LAKE, process_target=controlled_worker)
    await worker.start()
    active = asyncio.create_task(worker.call("probe", {"delay": 0.05}, "cancel", 1.0))
    await asyncio.sleep(0)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert (await worker.call("probe", {}, "next", 1.0))["request_id"] == "next"
    await worker.close()


@pytest.mark.asyncio
async def test_worker_network_is_disabled() -> None:
    worker = QueryWorker(LAKE, process_target=network_probe_worker)
    await worker.start()
    result = await worker.call("probe", {}, "network", 1.0)
    assert result["blocked"] is True
    assert result["native_blocked"] is (True if sys.platform.startswith("linux") else None)
    await worker.close()


@pytest.mark.asyncio
async def test_repeated_failures_open_restart_circuit_and_clear_readiness() -> None:
    worker = QueryWorker(
        LAKE,
        process_target=controlled_worker,
        max_restarts=1,
        restart_backoff_seconds=0,
    )
    await worker.start()
    with pytest.raises(ToolTimeoutError):
        await worker.call("probe", {"delay": 0.1}, "first", 0.01)
    assert worker.ready
    with pytest.raises(ToolTimeoutError):
        await worker.call("probe", {"delay": 0.1}, "second", 0.01)
    assert not worker.ready
    with pytest.raises(ToolUnavailableError):
        await worker.call("probe", {}, "circuit-open", 1.0)
    await worker.close()


@pytest.mark.asyncio
async def test_forced_shutdown_never_respawns_worker() -> None:
    worker = QueryWorker(
        LAKE,
        process_target=controlled_worker,
        restart_backoff_seconds=0,
    )
    await worker.start()
    active = asyncio.create_task(worker.call("probe", {"delay": 1.0}, "active", 2.0))
    await asyncio.sleep(0)
    await worker.close(drain_seconds=0.01)
    with pytest.raises(ToolUnavailableError):
        await active
    assert worker.pid is None
    assert not worker.ready


@pytest.mark.asyncio
async def test_real_worker_enforces_serialized_response_cap() -> None:
    worker = QueryWorker(LAKE, max_response_bytes=1024)
    await worker.start()
    try:
        with pytest.raises(ToolResourceLimitError):
            await worker.call("quant_list_series", {"limit": 100}, "large", 10.0)
    finally:
        await worker.close()
