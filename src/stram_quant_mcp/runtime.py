"""The assembled long-lived service components shared by the HTTP layer and the lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from stram_quant_mcp.audit import AuditJournal
from stram_quant_mcp.http_backend import DataBackend
from stram_quant_mcp.policy import InvocationService
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.telemetry import Telemetry


@dataclass(slots=True)
class Runtime:
    """The backend, audit journal, invocation service and telemetry of one running service."""

    settings: AppSettings
    backend: DataBackend
    audit: AuditJournal
    invocations: InvocationService
    telemetry: Telemetry
    accepting: bool = False

    @property
    def ready(self) -> bool:
        """Whether the service is accepting calls and every dependency is healthy."""
        return self.accepting and self.backend.ready and self.audit.healthy
