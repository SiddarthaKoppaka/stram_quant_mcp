from __future__ import annotations

import pytest
from mcp.server.auth.provider import AccessToken

from stram_quant_mcp.access_policy import (
    CallerIdentity,
    DenialReason,
    ScopeGrantStore,
    identity_from_token,
)
from stram_quant_mcp.tool_registry import TOOL_SPECS

ISSUER = "https://idp.test/realms/stram"
TENANT = "local-stram"


def _store() -> ScopeGrantStore:
    return ScopeGrantStore.from_tool_specs(issuer=ISSUER, tenant=TENANT)


def _identity(scopes: set[str], *, issuer: str = ISSUER, tenant: str = TENANT) -> CallerIdentity:
    return CallerIdentity(issuer, tenant, "client", "subject", frozenset(scopes))


def test_a_caller_with_the_tools_scope_is_allowed() -> None:
    assert _store().decide(_identity({"quant:discover"}), "quant_list_series").allowed


def test_a_caller_without_the_scope_is_denied() -> None:
    decision = _store().decide(_identity({"quant:read"}), "quant_list_series")

    assert not decision.allowed
    assert decision.reason is DenialReason.MISSING_SCOPE


@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        (None, DenialReason.UNAUTHENTICATED),
        (_identity({"quant:discover"}, issuer="https://evil.test"), DenialReason.WRONG_ISSUER),
        (_identity({"quant:discover"}, tenant="other"), DenialReason.WRONG_TENANT),
    ],
)
def test_anything_the_store_cannot_vouch_for_is_denied(
    identity: CallerIdentity | None, reason: DenialReason
) -> None:
    assert _store().decide(identity, "quant_list_series").reason is reason


def test_a_tool_with_no_grant_is_denied_even_with_every_scope() -> None:
    every_scope = {spec.scope for spec in TOOL_SPECS}

    decision = _store().decide(_identity(every_scope), "quant_delete_everything")

    assert decision.reason is DenialReason.NO_GRANT


def test_an_empty_store_denies_everything() -> None:
    store = ScopeGrantStore(issuer=ISSUER, tenant=TENANT, grants={})

    assert not store.decide(_identity({"quant:discover"}), "quant_list_series").allowed
    assert store.visible_tools(_identity({"quant:discover"})) == frozenset()


def test_visibility_matches_the_decision_for_every_tool() -> None:
    identity = _identity({"quant:discover"})
    store = _store()

    visible = store.visible_tools(identity)

    assert visible == {spec.name for spec in TOOL_SPECS if spec.scope == "quant:discover"}
    assert all(
        store.decide(identity, spec.name).allowed == (spec.name in visible) for spec in TOOL_SPECS
    )


def test_a_token_missing_issuer_or_tenant_gives_no_identity() -> None:
    bare = AccessToken(token="verified", client_id="c", scopes=["quant:read"], claims={})  # noqa: S106  # pragma: allowlist secret
    full = AccessToken(
        token="verified",  # noqa: S106  # pragma: allowlist secret
        client_id="c",
        scopes=["quant:read"],
        subject="s",
        claims={"iss": ISSUER, "tenant_id": TENANT},
    )

    assert identity_from_token(None, "tenant_id") is None
    assert identity_from_token(bare, "tenant_id") is None
    identity = identity_from_token(full, "tenant_id")
    assert identity == CallerIdentity(ISSUER, TENANT, "c", "s", frozenset({"quant:read"}))
