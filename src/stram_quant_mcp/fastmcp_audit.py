"""Terminal audit records for calls that end before the query worker is reached."""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import ValidationError

from stram_quant_mcp.access_policy import CallerIdentity
from stram_quant_mcp.audit import AuditEvent, AuditJournal, AuditWriteError
from stram_quant_mcp.errors import ToolUnavailableError
from stram_quant_mcp.request_context import RequestContext
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME

MAX_TOOL_NAME_LENGTH = 100


class PreDispatchAuditor:
    """Writes the single terminal audit record for a call refused before dispatch.

    Calls that reach the worker are audited by ``InvocationService``; this covers denials,
    throttling and invalid requests so every call still ends in exactly one record.
    """

    def __init__(
        self, settings: AppSettings, audit: AuditJournal, policy_version: Callable[[], str]
    ) -> None:
        """Create an auditor.

        Args:
            settings: Service settings, for the image digest.
            audit: The tamper-evident journal.
            policy_version: Returns the current availability-policy version.
        """
        self._settings = settings
        self._audit = audit
        self._policy_version = policy_version

    def principal_hash(self, identity: CallerIdentity | None) -> str | None:
        """Return the pseudonymous caller key, or None for an unidentified caller."""
        if identity is None:
            return None
        return self._audit.pseudonymize(identity.issuer, identity.client_id, identity.subject)

    def record(
        self,
        tool_name: str,
        identity: CallerIdentity | None,
        context: RequestContext,
        outcome: tuple[str, str, str | None],
    ) -> None:
        """Append one terminal record.

        Args:
            tool_name: The requested tool name; truncated to a safe length.
            identity: The verified caller, if any.
            context: Identifiers for the request being audited.
            outcome: ``(authorization_outcome, outcome, error_class)``.

        Raises:
            ToolUnavailableError: If the record cannot be written; the call must then fail closed.
        """
        authorization_outcome, result, error_class = outcome
        spec = TOOLS_BY_NAME.get(tool_name)
        try:
            self._audit.append(
                AuditEvent(
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    principal_hash=self.principal_hash(identity),
                    issuer=identity.issuer if identity else None,
                    tenant=identity.tenant if identity else None,
                    client_id=identity.client_id if identity else None,
                    tool=tool_name[:MAX_TOOL_NAME_LENGTH] or "unknown",
                    schema_hash=spec.schema_hash() if spec else None,
                    authorization_outcome=authorization_outcome,
                    required_scope=spec.scope if spec else None,
                    policy_version=self._policy_version(),
                    duration_ms=round((time.monotonic() - context.started) * 1000, 3),
                    outcome=result,
                    error_class=error_class,
                    image_digest=self._settings.image_digest,
                )
            )
        except (AuditWriteError, ValidationError) as error:
            raise ToolUnavailableError from error
