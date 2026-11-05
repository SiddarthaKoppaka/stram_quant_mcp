"""No token, claim value, argument value, exception text or path may reach any log or output."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stram_quant_mcp.fastmcp_auth import StrictVerifier
from stram_quant_mcp.fastmcp_server import create_fastmcp_application
from tests.parity.harness import ALL_SCOPES, HOST, ParityClient, TokenMinter, parity_settings
from tests.support.fake_data_plane import GOOD_TOKEN_PREFIX, FakeDataPlane

ATTACKER_AUDIENCE = "https://attacker-audience-marker.example/mcp"
SEEDED_ARGUMENT = "seeded-argument-marker"


def test_failures_leave_nothing_sensitive_in_logs_or_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    minter = TokenMinter()
    settings = parity_settings(tmp_path)
    verifier = StrictVerifier(settings, public_key=minter.public_key_pem())
    built = create_fastmcp_application(
        settings,
        token_verifier=verifier,
        audit_key=b"k" * 32,
        data_transport=FakeDataPlane(),
    )
    wrong_audience = minter.mint("quant:read", overrides={"aud": ATTACKER_AUDIENCE})
    narrow = minter.mint("quant:discover")
    full = minter.mint(" ".join(ALL_SCOPES))

    with TestClient(built.app, base_url=f"https://{HOST}") as client:
        parity = ParityClient(client, minter, settings.audit_db_path)
        parity.call_tool("quant_list_series", {}, wrong_audience)
        parity.call_tool("quant_get_series", {}, narrow)
        parity.call_tool("quant_list_series", {"limit": SEEDED_ARGUMENT}, full)
        parity.call_tool("quant_no_such_tool", {}, full)
        parity.call_tool("quant_list_series", {"limit": 1}, full)

    observed = caplog.text + "".join(capfd.readouterr())
    for secret in (wrong_audience, narrow, full, ATTACKER_AUDIENCE, SEEDED_ARGUMENT, str(tmp_path)):
        assert secret not in observed
    # The service's own Data Plane credentials must never be logged either.
    for secret in ("test-client-secret", GOOD_TOKEN_PREFIX):
        assert secret not in observed
    assert "Traceback" not in observed
