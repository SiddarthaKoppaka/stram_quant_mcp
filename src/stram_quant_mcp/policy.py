"""Per-invocation authorization, rate policy, safe dispatch, and terminal audit."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable

from mcp.server.auth.provider import AccessToken
from pydantic import JsonValue, ValidationError

from stram_quant_mcp.audit import AuditEvent, AuditJournal, AuditWriteError
from stram_quant_mcp.errors import (
    QuantToolError,
    ToolForbiddenError,
    ToolInputError,
    ToolOverloadedError,
    ToolUnavailableError,
)
from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME
from stram_quant_mcp.worker import QueryWorker


class RateLimiter:
    """Bounded in-memory fixed-window admission for a single V1 replica."""

    def __init__(
        self,
        limit_per_minute: int,
        max_principals: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit_per_minute
        self._max_principals = max_principals
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, principal: str) -> bool:
        now = self._clock()
        cutoff = now - 60.0
        with self._lock:
            calls = self._calls.get(principal)
            if calls is None:
                self._remove_idle(cutoff)
                if len(self._calls) >= self._max_principals:
                    return False
                calls = deque()
                self._calls[principal] = calls
            while calls and calls[0] <= cutoff:
                calls.popleft()
            if len(calls) >= self._limit:
                return False
            calls.append(now)
            return True

    def _remove_idle(self, cutoff: float) -> None:
        idle = [key for key, calls in self._calls.items() if not calls or calls[-1] <= cutoff]
        for key in idle:
            del self._calls[key]


class InvocationService:
    """Authorize and audit every terminal tool invocation around the query worker."""

    def __init__(
        self,
        settings: AppSettings,
        worker: QueryWorker,
        audit: AuditJournal,
        rate_limiter: RateLimiter,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._audit = audit
        self._rate_limiter = rate_limiter
        self._clock = clock

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        access_token: AccessToken | None,
        *,
        trace_id: str | None = None,
    ) -> dict[str, JsonValue]:
        started = self._clock()
        spec = TOOLS_BY_NAME.get(tool_name)
        issuer, tenant = _identity_claims(access_token, self._settings.tenant_claim)
        client_id = access_token.client_id if access_token else None
        subject = access_token.subject if access_token else None
        principal_hash = (
            self._audit.pseudonymize(issuer, client_id, subject) if issuer and client_id else None
        )
        base: dict[str, object] = {
            "request_id": request_id,
            "trace_id": trace_id,
            "principal_hash": principal_hash,
            "issuer": issuer,
            "tenant": tenant,
            "client_id": client_id,
            "tool": tool_name[:100] or "unknown",
            "schema_hash": spec.schema_hash() if spec else None,
            "required_scope": spec.scope if spec else None,
            "data_scope": _data_scope(arguments),
            "availability_mode": _string_value(arguments.get("availability")),
            "policy_version": self._worker.policy_version,
            "image_digest": self._settings.image_digest,
        }
        if spec is None:
            self._append(base, started, "denied", "invalid_tool", "ToolInputError")
            raise ToolInputError from None
        if (
            access_token is None
            or issuer != str(self._settings.issuer_url)
            or tenant != self._settings.tenant_id
            or spec.scope not in access_token.scopes
        ):
            self._append(base, started, "denied", "authorization_denied", "ToolForbiddenError")
            raise ToolForbiddenError from None
        if principal_hash is None or not self._rate_limiter.allow(principal_hash):
            self._append(base, started, "allowed", "rate_limited", "ToolOverloadedError")
            raise ToolOverloadedError from None
        try:
            payload = await self._worker.call(
                tool_name,
                arguments,
                request_id,
                spec.timeout_seconds,
            )
            validated = spec.output_model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            self._append(base, started, "allowed", "invalid_worker_output", "ValidationError")
            raise ToolUnavailableError from error
        except asyncio.CancelledError:
            self._append(base, started, "allowed", "cancelled", "CancelledError")
            raise
        except QuantToolError as error:
            self._append(base, started, "allowed", "error", error.__class__.__name__)
            raise
        except Exception as error:
            self._append(base, started, "allowed", "internal_error", error.__class__.__name__)
            raise ToolUnavailableError from error
        data = validated.get("data")
        row_count, truncated = _result_size(data)
        completed = {**base, "policy_version": str(validated.get("policy_version", "unknown"))}
        self._append(
            completed,
            started,
            "allowed",
            "success",
            None,
            row_count=row_count,
            truncated=truncated,
        )
        return validated

    def _append(
        self,
        base: dict[str, object],
        started: float,
        authorization_outcome: str,
        outcome: str,
        error_class: str | None,
        *,
        row_count: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        try:
            self._audit.append(
                AuditEvent.model_validate(
                    {
                        **base,
                        "authorization_outcome": authorization_outcome,
                        "duration_ms": round((self._clock() - started) * 1000, 3),
                        "outcome": outcome,
                        "error_class": error_class,
                        "row_count": row_count,
                        "truncated": truncated,
                    }
                )
            )
        except (AuditWriteError, ValidationError) as error:
            raise ToolUnavailableError from error


def _identity_claims(token: AccessToken | None, tenant_claim: str) -> tuple[str | None, str | None]:
    claims = token.claims or {} if token else {}
    issuer = claims.get("iss")
    tenant = claims.get(tenant_claim)
    return (
        str(issuer) if isinstance(issuer, str) else None,
        str(tenant) if isinstance(tenant, str) else None,
    )


def _data_scope(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    allowed = {
        "dataset",
        "series_keys",
        "indicator_ids",
        "indicator_id",
        "subject",
        "provider",
        "start_date",
        "end_date",
        "as_of",
        "availability",
        "target_symbol",
    }
    scope = {key: value for key, value in arguments.items() if key in allowed}
    columns = arguments.get("columns")
    if isinstance(columns, list):
        scope["columns"] = [
            {
                key: value
                for key, value in column.items()
                if key in {"name", "series_key", "indicator_id"}
            }
            for column in columns
            if isinstance(column, dict)
        ]
    conditions = arguments.get("conditions")
    if isinstance(conditions, list):
        scope["conditions"] = [
            {
                key: value
                for key, value in condition.items()
                if key in {"series_key", "indicator_id", "operator"}
            }
            for condition in conditions
            if isinstance(condition, dict)
        ]
    return scope


def _string_value(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _result_size(data: JsonValue | None) -> tuple[int | None, bool | None]:
    if not isinstance(data, dict):
        return None, None
    count = data.get("count", data.get("event_count"))
    if not isinstance(count, int):
        for field in ("rows", "entries", "indicators", "subjects", "policies", "datasets"):
            items = data.get(field)
            if isinstance(items, list):
                count = len(items)
                break
    truncated = data.get("truncated")
    return (
        int(count) if isinstance(count, int) else None,
        bool(truncated) if isinstance(truncated, bool) else None,
    )
