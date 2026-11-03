"""Audit behavior the service must preserve."""

from __future__ import annotations

from tests.parity.harness import ALL_SCOPES, ParityClient

DENIED = "authorization_denied"


def test_each_call_appends_exactly_one_terminal_record(parity: ParityClient) -> None:
    full = parity.minter.mint(" ".join(ALL_SCOPES))
    discover_only = parity.minter.mint("quant:discover")

    parity.call_tool("quant_list_series", {"limit": 1}, full)
    parity.call_tool("quant_get_series", {}, discover_only)
    parity.call_tool("quant_list_series", {"limit": "x"}, full)
    parity.call_tool("quant_delete_everything", {}, full)

    outcomes = parity.audit_outcomes()
    assert len(outcomes) == 4
    assert outcomes[0] == "success"
    assert outcomes[1] == DENIED


def test_a_transport_denial_is_audited_once(parity: ParityClient) -> None:
    parity.rpc("tools/list")

    assert len(parity.audit_outcomes()) == 1


def test_the_audit_journal_never_contains_the_token_or_paths(parity: ParityClient) -> None:
    token = parity.minter.mint(" ".join(ALL_SCOPES))

    parity.call_tool("quant_list_series", {"limit": 1}, token)
    journal = parity.audit_text()

    assert token not in journal
    assert "/home" not in journal
    assert "fixtures/lake" not in journal
