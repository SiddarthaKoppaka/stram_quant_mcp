"""Default-deny policy, hidden tools and denial throttling in the FastMCP server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stram_quant_mcp.access_policy import ScopeGrantStore
from stram_quant_mcp.errors import ToolForbiddenError
from stram_quant_mcp.fastmcp_auth import StrictVerifier
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from stram_quant_mcp.settings import AppSettings
from tests.parity.harness import (
    ALL_SCOPES,
    HOST,
    ISSUER,
    TENANT,
    ParityClient,
    TokenMinter,
    parity_settings,
)
from tests.support.fake_data_plane import FakeDataPlane

DENIAL_LIMIT = 3
ACCESS_DENIED = "Access denied."


def _open(
    tmp_path: Path, settings: AppSettings | None = None, **overrides: object
) -> Iterator[tuple[ParityClient, TokenMinter]]:
    minter = TokenMinter()
    settings = settings or parity_settings(tmp_path)
    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())
    built = create_fastmcp_application(
        settings,
        token_verifier=verifier,
        audit_key=b"k" * 32,
        data_transport=FakeDataPlane(),
        **overrides,
    )
    with TestClient(built.app, base_url=f"https://{HOST}") as client:
        yield ParityClient(client, minter, settings.audit_db_path), minter


@pytest.fixture
def empty_store_client(tmp_path: Path) -> Iterator[tuple[ParityClient, TokenMinter]]:
    store = ScopeGrantStore(issuer=ISSUER, tenant=TENANT, grants={})
    yield from _open(tmp_path, policy_store=store)


@pytest.fixture
def throttled_client(tmp_path: Path) -> Iterator[tuple[ParityClient, TokenMinter]]:
    settings = parity_settings(tmp_path).model_copy(update={"denial_limit": DENIAL_LIMIT})
    yield from _open(tmp_path, settings)


def test_a_caller_the_store_has_no_grant_for_is_denied_everything(
    empty_store_client: tuple[ParityClient, TokenMinter],
) -> None:
    client, minter = empty_store_client
    token = minter.mint(" ".join(ALL_SCOPES))

    listed = client.rpc("tools/list", token=token).result["tools"]
    called = client.call_tool("quant_list_series", {"limit": 1}, token).result

    assert listed == []
    assert called["isError"] is True
    assert called["content"][0]["text"].startswith(ACCESS_DENIED)
    assert called["_meta"]["io.stram/error"] == ToolForbiddenError().public_details()
    assert called["_meta"]["io.stram/error"]["category"] == "access_denied"


def test_a_denied_call_never_reaches_the_worker(
    empty_store_client: tuple[ParityClient, TokenMinter],
) -> None:
    client, minter = empty_store_client

    client.call_tool("quant_list_series", {"limit": 1}, minter.mint("quant:discover"))

    assert client.audit_outcomes() == ["authorization_denied"]


def test_repeated_denials_block_the_caller_from_every_tool(
    throttled_client: tuple[ParityClient, TokenMinter],
) -> None:
    client, minter = throttled_client
    token = minter.mint(" ".join(ALL_SCOPES))
    narrow = minter.mint("quant:discover")
    allowed_before = client.call_tool("quant_list_series", {"limit": 1}, token).result
    for _ in range(DENIAL_LIMIT):
        client.call_tool("quant_get_series", {}, narrow)

    blocked = client.call_tool("quant_list_series", {"limit": 1}, narrow).result

    assert allowed_before["isError"] is False
    assert blocked["content"][0]["text"].startswith(ACCESS_DENIED)
    assert client.audit_outcomes()[-1] == "denial_throttled"


def test_a_blocked_caller_sees_no_tools(
    throttled_client: tuple[ParityClient, TokenMinter],
) -> None:
    client, minter = throttled_client
    narrow = minter.mint("quant:discover")
    for _ in range(DENIAL_LIMIT):
        client.call_tool("quant_get_series", {}, narrow)

    assert client.rpc("tools/list", token=narrow).result["tools"] == []


def test_one_callers_denials_do_not_block_another(
    throttled_client: tuple[ParityClient, TokenMinter],
) -> None:
    client, minter = throttled_client
    attacker = minter.mint("quant:discover")
    other = minter.mint("quant:discover", overrides={"client_id": "other-client", "sub": "other"})
    for _ in range(DENIAL_LIMIT):
        client.call_tool("quant_get_series", {}, attacker)

    result = client.call_tool("quant_list_series", {"limit": 1}, other).result

    assert result["isError"] is False
