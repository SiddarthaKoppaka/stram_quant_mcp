"""Default-deny authorization decisions made from an explicit store of grants.

Every tool call is decided here, both to hide tools from a listing and to authorize the call itself,
so what a caller can see and what a caller can do cannot disagree. A caller that the store has no
grant for is denied; there is no implicit allow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from mcp.server.auth.provider import AccessToken

from stram_quant_mcp.tool_registry import TOOL_SPECS


class DenialReason(Enum):
    """Why a call was denied; used for audit and throttling, never sent to the caller."""

    UNAUTHENTICATED = "unauthenticated"
    UNKNOWN_TOOL = "unknown_tool"
    NO_GRANT = "no_grant"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_TENANT = "wrong_tenant"
    MISSING_SCOPE = "missing_scope"
    THROTTLED = "throttled"


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Verified facts about the caller, taken only from a verified token."""

    issuer: str
    tenant: str
    client_id: str
    subject: str | None
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """The outcome of one authorization check."""

    allowed: bool
    reason: DenialReason | None = None


ALLOWED = AccessDecision(allowed=True)


class AccessPolicyStore(Protocol):
    """Source of truth for who may call which tool."""

    def decide(self, identity: CallerIdentity | None, tool_name: str) -> AccessDecision:
        """Return whether ``identity`` may call ``tool_name``."""

    def visible_tools(self, identity: CallerIdentity | None) -> frozenset[str]:
        """Return the tools ``identity`` may call, and so may see listed."""


class ScopeGrantStore:
    """Grants each tool to callers of one issuer and tenant that hold the tool's scope."""

    def __init__(self, *, issuer: str, tenant: str, grants: Mapping[str, str]) -> None:
        """Create a store.

        Args:
            issuer: The only token issuer whose callers can be granted anything.
            tenant: The only tenant whose callers can be granted anything.
            grants: Tool name to the single scope that authorizes it.
        """
        self._issuer = issuer
        self._tenant = tenant
        self._grants = dict(grants)

    @classmethod
    def from_tool_specs(cls, *, issuer: str, tenant: str) -> ScopeGrantStore:
        """Build the store from the reviewed tool catalogue."""
        return cls(
            issuer=issuer,
            tenant=tenant,
            grants={spec.name: spec.scope for spec in TOOL_SPECS},
        )

    def decide(self, identity: CallerIdentity | None, tool_name: str) -> AccessDecision:
        """Deny unless the store holds a grant that this caller satisfies."""
        required_scope = self._grants.get(tool_name)
        reason = self._denial_reason(identity, required_scope)
        return ALLOWED if reason is None else AccessDecision(allowed=False, reason=reason)

    def visible_tools(self, identity: CallerIdentity | None) -> frozenset[str]:
        """Return every tool this caller is allowed to call."""
        return frozenset(name for name in self._grants if self.decide(identity, name).allowed)

    def _denial_reason(
        self, identity: CallerIdentity | None, required_scope: str | None
    ) -> DenialReason | None:
        if identity is None:
            return DenialReason.UNAUTHENTICATED
        if required_scope is None:
            return DenialReason.NO_GRANT
        if identity.issuer != self._issuer:
            return DenialReason.WRONG_ISSUER
        if identity.tenant != self._tenant:
            return DenialReason.WRONG_TENANT
        if required_scope not in identity.scopes:
            return DenialReason.MISSING_SCOPE
        return None


def identity_from_token(token: AccessToken | None, tenant_claim: str) -> CallerIdentity | None:
    """Reduce a verified token to a caller identity, or None if any required fact is absent."""
    if token is None:
        return None
    claims = token.claims or {}
    issuer = claims.get("iss")
    tenant = claims.get(tenant_claim)
    if not isinstance(issuer, str) or not isinstance(tenant, str) or not token.client_id:
        return None
    return CallerIdentity(
        issuer=issuer,
        tenant=tenant,
        client_id=token.client_id,
        subject=token.subject,
        scopes=frozenset(token.scopes),
    )
