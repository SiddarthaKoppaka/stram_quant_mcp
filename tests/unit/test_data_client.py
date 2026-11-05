"""The Data Plane client must classify every failure by cause, retryability and next step."""

from __future__ import annotations

import httpx
import pytest

from stram_quant_mcp import errors
from stram_quant_mcp.data_client import DataPlaneClient, DataPlaneSettings
from tests.support.fake_data_plane import DATA_URL, TOKEN_URL, FakeDataPlane, problem

CATALOG = "/v1/catalog"
PARAMS = {"limit": 1}


def _client(
    plane: FakeDataPlane,
    secret_file,
    clock=None,
    **settings: object,  # type: ignore[no-untyped-def]
) -> DataPlaneClient:
    values = DataPlaneSettings(
        base_url=DATA_URL,
        token_url=TOKEN_URL,
        client_id="stram-quant-mcp-test",
        client_secret_path=secret_file,
        **settings,  # type: ignore[arg-type]
    )
    return DataPlaneClient(values, transport=plane, **({"clock": clock} if clock else {}))


async def _get(client: DataPlaneClient) -> bytes:
    return await client.get_json(CATALOG, params=PARAMS)


@pytest.mark.asyncio
async def test_a_successful_call_returns_the_raw_body(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    body = await _get(_client(data_plane, secret_file))

    assert body.startswith(b"[")


@pytest.mark.asyncio
async def test_the_token_is_cached_across_calls_and_never_sent_in_the_query(  # type: ignore[no-untyped-def]
    data_plane, secret_file
) -> None:
    client = _client(data_plane, secret_file)

    for _ in range(3):
        await _get(client)

    assert len(data_plane.token_requests) == 1
    assert data_plane.token_requests[0]["grant_type"] == "client_credentials"
    assert all("service-token" not in str(r.url) for r in data_plane.calls_to(CATALOG))


@pytest.mark.asyncio
async def test_the_token_is_refreshed_shortly_before_it_expires(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    now = [0.0]
    client = _client(data_plane, secret_file, clock=lambda: now[0])

    await _get(client)
    now[0] = 200.0
    await _get(client)
    assert len(data_plane.token_requests) == 1
    now[0] = 280.0  # inside the 30s refresh margin of a 300s token
    await _get(client)

    assert len(data_plane.token_requests) == 2


@pytest.mark.asyncio
async def test_a_rejected_token_is_replaced_once_and_the_call_succeeds(  # type: ignore[no-untyped-def]
    data_plane, secret_file
) -> None:
    client = _client(data_plane, secret_file)
    await _get(client)
    data_plane.revoked.add("service-token-1")

    body = await _get(client)

    assert body.startswith(b"[")
    assert len(data_plane.token_requests) == 2


@pytest.mark.asyncio
async def test_a_token_rejected_twice_means_the_service_credentials_are_wrong(  # type: ignore[no-untyped-def]
    data_plane, secret_file
) -> None:
    data_plane.script(
        lambda r: (
            problem(401, "unauthenticated", "token rejected") if "/v1/" in r.url.path else None
        )
    )

    with pytest.raises(errors.ToolCredentialsError) as raised:
        await _get(_client(data_plane, secret_file))

    assert raised.value.retryable is False
    assert raised.value.category is errors.ErrorCategory.SERVICE_CREDENTIALS
    assert "operator" in raised.value.next_action
    assert len(data_plane.token_requests) == 2


@pytest.mark.asyncio
async def test_a_missing_scope_is_named_and_is_an_operator_problem(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    data_plane.granted_scopes.discard("catalog:read")

    with pytest.raises(errors.ToolUpstreamForbiddenError) as raised:
        await _get(_client(data_plane, secret_file))

    assert "catalog:read" in raised.value.reason
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_an_invalid_query_carries_the_data_planes_reason(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    client = _client(data_plane, secret_file)
    body = {"series_keys": ["SPY"], "start_date": "2010-01-01", "end_date": "2026-01-01"}

    with pytest.raises(errors.ToolInputError) as raised:
        await client.post_json("/v1/series/query", body)

    assert raised.value.retryable is False
    assert raised.value.category is errors.ErrorCategory.CALLER_ERROR
    assert raised.value.upstream_request_id


@pytest.mark.asyncio
async def test_an_unknown_entity_is_not_found_and_not_retryable(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    client = _client(data_plane, secret_file)

    with pytest.raises(errors.ToolNotFoundError) as raised:
        await client.get_json("/v1/indicators/no_such_indicator/subjects")

    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_paths_and_control_characters_never_reach_the_caller(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    data_plane.script(
        lambda r: (
            problem(422, "invalid_query", "bad \x00 file /lake/econ/part-0.parquet here")
            if r.url.path == CATALOG
            else None
        )
    )

    with pytest.raises(errors.ToolInputError) as raised:
        await _get(_client(data_plane, secret_file))

    assert "/lake" not in raised.value.reason
    assert "\x00" not in raised.value.reason
    assert "[path]" in raised.value.reason


@pytest.mark.asyncio
async def test_an_outage_is_retryable_and_honours_retry_after(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    data_plane.script(
        lambda r: (
            problem(503, "data_unavailable", "lake at /lake/x", **{"Retry-After": "7"})
            if r.url.path == CATALOG
            else None
        )
    )

    with pytest.raises(errors.ToolUnavailableError) as raised:
        await _get(_client(data_plane, secret_file))

    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds == 7
    assert "/lake" not in raised.value.caller_text


@pytest.mark.parametrize("status", [502, 504, 429])
@pytest.mark.asyncio
async def test_gateway_and_rate_limit_failures_are_retryable(  # type: ignore[no-untyped-def]
    data_plane, secret_file, status: int
) -> None:
    data_plane.script(lambda r: httpx.Response(status) if r.url.path == CATALOG else None)

    with pytest.raises(errors.ToolUnavailableError) as raised:
        await _get(_client(data_plane, secret_file))

    assert str(status) in raised.value.reason
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_a_data_plane_bug_is_reported_with_its_request_id_and_not_retried(  # type: ignore[no-untyped-def]
    data_plane, secret_file
) -> None:
    data_plane.script(
        lambda r: (
            problem(500, "internal_error", "boom /secret/path") if r.url.path == CATALOG else None
        )
    )

    with pytest.raises(errors.ToolUpstreamError) as raised:
        await _get(_client(data_plane, secret_file))

    assert raised.value.retryable is False
    assert raised.value.upstream_request_id == "req-test-1"
    assert "/secret" not in raised.value.caller_text
    assert len(data_plane.calls_to(CATALOG)) == 1


@pytest.mark.asyncio
async def test_a_slow_data_plane_is_a_retryable_deadline_error(data_plane, secret_file) -> None:  # type: ignore[no-untyped-def]
    def slow(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == CATALOG:
            raise httpx.ReadTimeout("slow", request=request)
        return None

    data_plane.script(slow)

    with pytest.raises(errors.ToolTimeoutError) as raised:
        await _get(_client(data_plane, secret_file))

    assert raised.value.retryable is True
    assert "narrower" in raised.value.next_action


@pytest.mark.asyncio
async def test_one_connection_reset_is_retried_but_two_are_an_outage(
    data_plane, secret_file
) -> None:  # type: ignore[no-untyped-def]
    resets = {"left": 1}

    def flaky(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == CATALOG and resets["left"]:
            resets["left"] -= 1
            raise httpx.ConnectError("reset", request=request)
        return None

    data_plane.script(flaky)
    client = _client(data_plane, secret_file)
    assert (await _get(client)).startswith(b"[")

    resets["left"] = 2
    with pytest.raises(errors.ToolUnavailableError) as raised:
        await _get(client)

    assert raised.value.retryable is True
    assert "twice" in raised.value.reason


@pytest.mark.asyncio
async def test_an_oversized_response_is_a_resource_limit_not_a_retry(
    data_plane, secret_file
) -> None:  # type: ignore[no-untyped-def]
    client = _client(data_plane, secret_file, max_response_bytes=1024)
    data_plane.script(
        lambda r: (
            httpx.Response(200, content=b"[" + b"1," * 2000 + b"1]")
            if r.url.path == CATALOG
            else None
        )
    )

    with pytest.raises(errors.ToolResourceLimitError) as raised:
        await _get(client)

    assert raised.value.retryable is False
    assert "Reduce" in raised.value.next_action


@pytest.mark.asyncio
async def test_hindsight_reads_use_a_separate_token_with_the_extra_scope(  # type: ignore[no-untyped-def]
    data_plane, secret_file
) -> None:
    client = _client(data_plane, secret_file)
    body = {"series_keys": ["SPY"], "start_date": "2026-10-01", "end_date": "2026-10-02"}

    await client.post_json("/v1/series/query", body)
    await client.post_json(
        "/v1/series/query", {**body, "availability": "ex_post"}, extra_scope="hindsight:read"
    )

    scopes = [form.get("scope") for form in data_plane.token_requests]
    assert scopes == [None, "hindsight%3Aread"]


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (400, errors.ToolCredentialsError, False),
        (401, errors.ToolCredentialsError, False),
        (503, errors.ToolUnavailableError, True),
        (500, errors.ToolUnavailableError, True),
    ],
)
@pytest.mark.asyncio
async def test_identity_provider_failures_are_classified(  # type: ignore[no-untyped-def]
    data_plane, secret_file, status: int, category: type, retryable: bool
) -> None:
    data_plane.token_status = status

    with pytest.raises(category) as raised:
        await _get(_client(data_plane, secret_file))

    assert raised.value.retryable is retryable


@pytest.mark.asyncio
async def test_a_missing_client_secret_file_is_a_credentials_error(data_plane, tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client(data_plane, tmp_path / "absent.secret")

    with pytest.raises(errors.ToolCredentialsError) as raised:
        await _get(client)

    assert str(tmp_path) not in raised.value.caller_text


@pytest.mark.asyncio
async def test_an_unreachable_identity_provider_is_a_retryable_outage(
    data_plane, secret_file
) -> None:  # type: ignore[no-untyped-def]
    def down(request: httpx.Request) -> httpx.Response | None:
        raise httpx.ConnectError("refused", request=request)

    data_plane.script(down)

    with pytest.raises(errors.ToolUnavailableError):
        await _get(_client(data_plane, secret_file))
