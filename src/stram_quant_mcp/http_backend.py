"""Bounded execution of reviewed tool calls against the Data Plane service.

This replaces the former spawned query process. Admission, deadlines and failure containment are
enforced here; identity, authorization and audit stay in the invocation service around it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import JsonValue
from stram_data.data_interface import INTERFACE_VERSION

from stram_quant_mcp.data_client import DataPlaneClient
from stram_quant_mcp.data_gateway import DataGateway
from stram_quant_mcp.errors import (
    QuantToolError,
    ToolCredentialsError,
    ToolOverloadedError,
    ToolTimeoutError,
    ToolUnavailableError,
    ToolUpstreamError,
    ToolUpstreamForbiddenError,
)
from stram_quant_mcp.tool_adapter import QuantToolAdapter

MINIMUM_SERVICE_VERSION: Final = (1, 9)
EXPECTED_INTERFACE_VERSION: Final = INTERFACE_VERSION
STARTUP_TIMEOUT_SECONDS: Final = 15.0
MAX_REQUEST_ID_CHARS: Final = 128
_VERSION: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
# Failures that say the Data Plane path is unhealthy, as opposed to a bad request.
_BREAKER_ERRORS: Final = (
    ToolUnavailableError,
    ToolTimeoutError,
    ToolCredentialsError,
    ToolUpstreamForbiddenError,
    ToolUpstreamError,
)


class DataBackend(Protocol):
    """What the invocation service and lifespan need from a source of tool results."""

    @property
    def ready(self) -> bool:
        """Whether the backend can accept calls."""

    @property
    def policy_version(self) -> str:
        """Hash of the availability policies in force."""

    @property
    def dependency_versions_match(self) -> bool:
        """Whether the Data Plane versions are compatible."""

    async def start(self) -> None:
        """Verify the Data Plane and become ready."""

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        timeout_seconds: float,
    ) -> dict[str, JsonValue]:
        """Run one tool call and return its JSON envelope."""

    async def close(self, drain_seconds: float = 5.0) -> None:
        """Stop accepting calls and release resources."""


class CircuitBreaker:
    """Opens after consecutive Data Plane failures and sheds load until a cooldown passes."""

    def __init__(
        self,
        threshold: int,
        cooldown_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Open after ``threshold`` consecutive failures for ``cooldown_seconds``."""
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def remaining_seconds(self) -> int:
        """Seconds until a probe call is allowed, or 0 if calls may proceed."""
        if self._opened_at is None:
            return 0
        left = self._cooldown - (self._clock() - self._opened_at)
        if left <= 0:
            # Half-open: let the next call probe; one more failure re-opens immediately.
            self._opened_at = None
            self._failures = self._threshold - 1
            return 0
        return max(1, int(left + 0.999))

    def success(self) -> None:
        """Record a healthy Data Plane answer."""
        self._failures = 0
        self._opened_at = None

    def failure(self) -> None:
        """Record a Data Plane failure."""
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        """Whether calls are currently being shed."""
        return self._opened_at is not None


@dataclass(frozen=True, slots=True)
class BackendLimits:
    """Admission and failure-containment bounds for the Data Plane path."""

    max_concurrent: int = 4
    queue_wait_seconds: float = 2.0
    breaker_threshold: int = 5
    breaker_cooldown_seconds: float = 15.0


class HttpDataBackend:
    """Run tool calls through the typed gateway with bounded concurrency and hard deadlines."""

    def __init__(
        self,
        client: DataPlaneClient,
        limits: BackendLimits = BackendLimits(),  # noqa: B008 - frozen, immutable default
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Serve tool calls through ``client`` within ``limits``."""
        if (
            limits.max_concurrent < 1
            or limits.queue_wait_seconds <= 0
            or limits.breaker_threshold < 1
        ):
            raise ValueError("invalid backend limits")
        self._client = client
        self._gateway = DataGateway(client)
        self._max_concurrent = limits.max_concurrent
        self._queue_wait_seconds = limits.queue_wait_seconds
        self._slots = asyncio.BoundedSemaphore(limits.max_concurrent)
        self._breaker = CircuitBreaker(
            limits.breaker_threshold, limits.breaker_cooldown_seconds, clock=clock
        )
        self._adapter: QuantToolAdapter | None = None
        self._policy_version = "unknown"
        self._service_version = "unknown"
        self._interface_version = "unknown"
        self._closing = False

    @property
    def ready(self) -> bool:
        """Whether the backend can accept calls."""
        return self._adapter is not None and not self._closing

    @property
    def policy_version(self) -> str:
        """Hash of the availability policies in force."""
        return self._policy_version

    @property
    def circuit_open(self) -> bool:
        """Whether the circuit breaker is open."""
        return self._breaker.is_open

    @property
    def dependency_versions_match(self) -> bool:
        """Whether the Data Plane versions are compatible."""
        parsed = _VERSION.match(self._service_version)
        if parsed is None:
            return False
        version = (int(parsed.group(1)), int(parsed.group(2)))
        return (
            version[0] == MINIMUM_SERVICE_VERSION[0]
            and version >= MINIMUM_SERVICE_VERSION
            and self._interface_version == EXPECTED_INTERFACE_VERSION
        )

    async def start(self) -> None:
        """Verify the Data Plane is reachable, compatible and entitled before accepting calls."""
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
                service, interface = await self._gateway.service_versions()
                self._service_version, self._interface_version = service, interface
                if not self.dependency_versions_match:
                    raise ToolUpstreamError(
                        f"Data Plane {service} / interface v{interface} is not compatible; "
                        f"need >= {MINIMUM_SERVICE_VERSION[0]}.{MINIMUM_SERVICE_VERSION[1]} "
                        f"and interface v{EXPECTED_INTERFACE_VERSION}."
                    )
                policies = await self._gateway.describe_availability_policies(deadline_seconds=10.0)
        except TimeoutError:
            raise ToolTimeoutError("The Data Plane did not answer during startup.") from None
        if not policies:
            raise ToolUpstreamError("The Data Plane reported no availability policies.")
        described = [item.model_dump(mode="json") for item in policies]
        encoded = json.dumps(described, sort_keys=True, separators=(",", ":")).encode()
        self._policy_version = hashlib.sha256(encoded).hexdigest()[:16]
        self._adapter = QuantToolAdapter(
            self._gateway, policy_version=self._policy_version, service_version=service
        )

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        timeout_seconds: float,
    ) -> dict[str, JsonValue]:
        """Run one tool call and return its JSON envelope."""
        adapter = self._adapter
        if self._closing or adapter is None:
            raise ToolUnavailableError(
                "The service is starting up or shutting down and is not accepting calls.",
                retry_after_seconds=5,
            )
        if timeout_seconds <= 0 or len(request_id) > MAX_REQUEST_ID_CHARS:
            raise ToolUpstreamError("The service rejected an internal call configuration.")
        wait = self._breaker.remaining_seconds()
        if wait:
            raise ToolUnavailableError(
                "The Data Plane failed several calls in a row, so this service is pausing "
                "calls to it briefly (circuit open).",
                retry_after_seconds=wait,
            )
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self._queue_wait_seconds)
        except TimeoutError:
            raise ToolOverloadedError(
                f"All {self._max_concurrent} query slots stayed busy for "
                f"{self._queue_wait_seconds:g}s.",
                retry_after_seconds=2,
            ) from None
        try:
            return await self._run(adapter, tool_name, arguments, request_id, timeout_seconds)
        finally:
            self._slots.release()

    async def _run(
        self,
        adapter: QuantToolAdapter,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        timeout_seconds: float,
    ) -> dict[str, JsonValue]:
        try:
            async with asyncio.timeout(timeout_seconds):
                envelope = await adapter.execute(tool_name, arguments, request_id, timeout_seconds)
        except TimeoutError:
            self._breaker.failure()
            raise ToolTimeoutError(
                f"The Data Plane did not finish within {timeout_seconds:g}s for this tool."
            ) from None
        except _BREAKER_ERRORS:
            self._breaker.failure()
            raise
        except QuantToolError:
            # The Data Plane answered correctly, even if the answer was "no" for this request.
            self._breaker.success()
            raise
        self._breaker.success()
        payload: dict[str, JsonValue] = envelope.model_dump(mode="json")
        return payload

    async def close(self, drain_seconds: float = 5.0) -> None:
        """Stop accepting calls and release resources."""
        self._closing = True
        try:
            async with asyncio.timeout(drain_seconds):
                for _ in range(self._max_concurrent):
                    await self._slots.acquire()
        except TimeoutError:
            pass
        await self._client.aclose()
