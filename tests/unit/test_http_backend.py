"""Admission, deadlines, circuit breaking and startup checks of the Data Plane backend."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from stram_quant_mcp import errors
from stram_quant_mcp.data_client import DataPlaneClient
from stram_quant_mcp.http_backend import BackendLimits, CircuitBreaker, HttpDataBackend
from tests.support.fake_data_plane import FakeDataPlane, problem

CATALOG_ARGS = {"limit": 1}


def _fail_catalog(plane: FakeDataPlane, status: int = 503) -> None:
    plane.script(
        lambda r: (
            problem(status, "data_unavailable", "down") if r.url.path == "/v1/catalog" else None
        )
    )


async def _call(backend: HttpDataBackend, deadline: float = 5.0) -> dict[str, object]:
    return await backend.call("quant_list_series", CATALOG_ARGS, "req-1", deadline)


@pytest.mark.asyncio
async def test_startup_records_versions_and_policy_hash(backend: HttpDataBackend) -> None:
    assert backend.ready is True
    assert backend.dependency_versions_match is True
    assert len(backend.policy_version) == 16


@pytest.mark.asyncio
async def test_the_envelope_carries_the_data_plane_version_not_a_stale_constant(
    backend: HttpDataBackend,
) -> None:
    result = await _call(backend)

    assert result["stram_data_version"] == "1.9.0"
    assert result["interface_version"] == "1"
    assert result["policy_version"] == backend.policy_version
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_calls_before_startup_are_a_retryable_unavailable(client: DataPlaneClient) -> None:
    backend = HttpDataBackend(client)

    with pytest.raises(errors.ToolUnavailableError) as raised:
        await _call(backend)

    assert raised.value.retryable is True
    assert backend.ready is False


@pytest.mark.asyncio
async def test_an_incompatible_data_plane_refuses_to_start(
    client: DataPlaneClient, data_plane: FakeDataPlane
) -> None:
    def old_version(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/v1/openapi.json":
            return None
        info = {"title": "x", "version": "1.3.1", "description": "Data Interface v1."}
        return httpx.Response(200, json={"info": info})

    data_plane.script(old_version)
    backend = HttpDataBackend(client)

    with pytest.raises(errors.ToolUpstreamError) as raised:
        await backend.start()

    assert "not compatible" in raised.value.reason

    assert backend.ready is False


@pytest.mark.asyncio
async def test_a_slow_query_is_cut_off_at_its_deadline(
    backend: HttpDataBackend, data_plane: FakeDataPlane
) -> None:
    async def never(_: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError

    original = data_plane.handle_async_request

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog":
            return await never(request)
        return await original(request)

    data_plane.handle_async_request = slow  # type: ignore[method-assign]

    with pytest.raises(errors.ToolTimeoutError) as raised:
        await _call(backend, deadline=0.05)

    assert raised.value.retryable is True
    assert "0.05" in raised.value.reason


@pytest.mark.asyncio
async def test_extra_calls_are_shed_as_overload_when_all_slots_stay_busy(
    client: DataPlaneClient, data_plane: FakeDataPlane
) -> None:
    backend = HttpDataBackend(client, BackendLimits(max_concurrent=1, queue_wait_seconds=0.05))
    await backend.start()
    gate = asyncio.Event()
    original = data_plane.handle_async_request

    async def gated(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog":
            await gate.wait()
        return await original(request)

    data_plane.handle_async_request = gated  # type: ignore[method-assign]
    first = asyncio.create_task(_call(backend))
    await asyncio.sleep(0.02)

    with pytest.raises(errors.ToolOverloadedError) as raised:
        await _call(backend)

    gate.set()
    await first
    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds
    assert "busy" in raised.value.reason


@pytest.mark.asyncio
async def test_repeated_outages_open_the_breaker_and_stop_hitting_the_data_plane(
    client: DataPlaneClient, data_plane: FakeDataPlane
) -> None:
    backend = HttpDataBackend(
        client, BackendLimits(breaker_threshold=3, breaker_cooldown_seconds=60)
    )
    await backend.start()
    _fail_catalog(data_plane)
    for _ in range(3):
        with pytest.raises(errors.ToolUnavailableError):
            await _call(backend)
    calls_when_open = len(data_plane.calls_to("/v1/catalog"))

    with pytest.raises(errors.ToolUnavailableError) as raised:
        await _call(backend)

    assert "circuit open" in raised.value.reason
    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds
    assert len(data_plane.calls_to("/v1/catalog")) == calls_when_open
    assert backend.circuit_open is True


@pytest.mark.asyncio
async def test_caller_mistakes_never_trip_the_breaker(
    client: DataPlaneClient,
) -> None:
    backend = HttpDataBackend(client, BackendLimits(breaker_threshold=2))
    await backend.start()

    for _ in range(5):
        with pytest.raises(errors.ToolInputError):
            await backend.call("quant_list_series", {"limit": 99999}, "req-1", 5.0)
    result = await _call(backend)

    assert backend.circuit_open is False
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_a_not_found_answer_counts_as_a_healthy_data_plane(
    client: DataPlaneClient,
) -> None:
    backend = HttpDataBackend(client, BackendLimits(breaker_threshold=2))
    await backend.start()

    for _ in range(4):
        with pytest.raises(errors.ToolNotFoundError):
            await backend.call(
                "quant_list_indicator_subjects", {"indicator_id": "nope"}, "req-1", 5.0
            )

    assert backend.circuit_open is False


def test_breaker_half_opens_after_the_cooldown_and_reopens_on_one_more_failure() -> None:
    now = [0.0]
    breaker = CircuitBreaker(2, 10.0, clock=lambda: now[0])
    breaker.failure()
    breaker.failure()
    assert breaker.remaining_seconds() == 10

    now[0] = 11.0
    assert breaker.remaining_seconds() == 0
    breaker.failure()

    assert breaker.remaining_seconds() > 0


def test_a_success_resets_the_breaker() -> None:
    breaker = CircuitBreaker(2, 10.0)
    breaker.failure()
    breaker.success()
    breaker.failure()

    assert breaker.remaining_seconds() == 0
    assert breaker.is_open is False


@pytest.mark.asyncio
async def test_after_close_no_call_is_accepted(backend: HttpDataBackend) -> None:
    await backend.close(drain_seconds=1.0)

    with pytest.raises(errors.ToolUnavailableError):
        await _call(backend)
