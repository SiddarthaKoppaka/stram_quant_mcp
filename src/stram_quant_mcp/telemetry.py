"""Allowlisted structured logs, bounded metrics, and OpenTelemetry lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Final

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Tracer

from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.tool_registry import TOOLS_BY_NAME
from stram_quant_mcp.version import __version__

LOGGER_NAME: Final = "stram_quant_mcp.events"
SAFE_OUTCOMES: Final = {
    "success",
    "error",
    "denied",
    "overloaded",
    "unavailable",
    "invalid_request",
}
SAFE_MODES: Final = {"strict", "estimated", "ex_post", "none"}


class JsonEventLogger:
    """Serialize only a fixed safe field set; payloads and free-form exceptions are impossible."""

    def __init__(self, logger: logging.Logger | None) -> None:
        self._logger = logger

    def emit(
        self,
        event: str,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        request_id: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        duration_ms: float | None = None,
        error_class: str | None = None,
        status_code: int | None = None,
    ) -> None:
        if self._logger is None:
            return
        record: dict[str, str | int | float] = {"event": _safe_text(event, 80)}
        optional: Mapping[str, str | int | float | None] = {
            "trace_id": trace_id,
            "span_id": span_id,
            "request_id": request_id,
            "tool": _metric_tool(tool) if tool else None,
            "outcome": _metric_outcome(outcome) if outcome else None,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
            "error_class": _safe_text(error_class, 80) if error_class else None,
            "status_code": status_code,
        }
        record.update({key: value for key, value in optional.items() if value is not None})
        self._logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))


class Telemetry:
    """Low-cardinality service instruments with bounded best-effort exporter shutdown."""

    def __init__(
        self,
        tracer: Tracer,
        meter: Meter,
        event_logger: JsonEventLogger,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        shutdown_seconds: float = 5.0,
    ) -> None:
        self.tracer = tracer
        self.events = event_logger
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._shutdown_seconds = shutdown_seconds
        self._tool_count: Counter = meter.create_counter(
            "stram.quant.tool.invocations", unit="{invocation}"
        )
        self._tool_duration: Histogram = meter.create_histogram(
            "stram.quant.tool.duration", unit="s"
        )
        self._http_count: Counter = meter.create_counter(
            "stram.quant.http.requests", unit="{request}"
        )
        self._http_duration: Histogram = meter.create_histogram(
            "stram.quant.http.server.duration", unit="s"
        )
        self._readiness: UpDownCounter = meter.create_up_down_counter(
            "stram.quant.readiness", unit="{state}"
        )
        self._ready_value = 0

    @classmethod
    def disabled(cls) -> Telemetry:
        return cls(
            trace.get_tracer("stram_quant_mcp"),
            metrics.get_meter("stram_quant_mcp"),
            JsonEventLogger(None),
        )

    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, str | int | float | bool] | None = None,
        context: Context | None = None,
    ) -> AbstractContextManager[Span]:
        return self.tracer.start_as_current_span(
            name,
            kind=kind,
            attributes=dict(attributes or {}),
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        )

    def tool_result(
        self, tool: str, outcome: str, mode: str | None, duration_seconds: float
    ) -> None:
        attributes: Mapping[str, str] = {
            "tool": _metric_tool(tool),
            "outcome": _metric_outcome(outcome),
            "availability_mode": mode if mode in SAFE_MODES else "none",
        }
        self._tool_count.add(1, attributes)
        self._tool_duration.record(max(duration_seconds, 0), attributes)

    def http_result(self, method: str, status_code: int, duration_seconds: float) -> None:
        attributes: Mapping[str, str | int] = {
            "http.request.method": method if method in {"GET", "POST", "DELETE"} else "OTHER",
            "http.response.status_code": status_code,
        }
        self._http_count.add(1, attributes)
        self._http_duration.record(max(duration_seconds, 0), attributes)

    def set_ready(self, ready: bool) -> None:
        target = 1 if ready else 0
        delta = target - self._ready_value
        if delta:
            self._readiness.add(delta)
            self._ready_value = target

    async def shutdown(self) -> None:
        providers = tuple(
            provider
            for provider in (self._meter_provider, self._tracer_provider)
            if provider is not None
        )
        for provider in providers:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(provider.shutdown),
                    timeout=self._shutdown_seconds,
                )
            except (TimeoutError, RuntimeError):
                self.events.emit("telemetry.shutdown_failed", error_class="ExporterShutdownError")


def configure_telemetry(settings: AppSettings) -> Telemetry:
    """Configure OTLP/HTTP exporters once during production application construction."""

    resource = Resource.create(
        {
            "service.name": "stram-quant",
            "service.version": __version__,
            "deployment.environment.name": "private",
        }
    )
    meter_provider: MeterProvider
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    if settings.otlp_endpoint is None:
        meter_provider = MeterProvider(resource=resource, shutdown_on_exit=False)
    else:
        base = str(settings.otlp_endpoint).rstrip("/")
        timeout = settings.otel_export_timeout_seconds
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=timeout),
                max_queue_size=512,
                max_export_batch_size=128,
                export_timeout_millis=timeout * 1000,
            )
        )
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=timeout),
            export_interval_millis=settings.otel_metric_interval_seconds * 1000,
            export_timeout_millis=timeout * 1000,
        )
        meter_provider = MeterProvider(
            metric_readers=(metric_reader,), resource=resource, shutdown_on_exit=False
        )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    logger = _stdout_json_logger()
    return Telemetry(
        trace.get_tracer("stram_quant_mcp", __version__),
        metrics.get_meter("stram_quant_mcp", __version__),
        JsonEventLogger(logger),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        shutdown_seconds=settings.telemetry_shutdown_seconds,
    )


def correlation_ids() -> tuple[str | None, str | None]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def _stdout_json_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _safe_text(value: str, limit: int) -> str:
    return "".join(character for character in value[:limit] if character.isprintable())


def _metric_tool(tool: str | None) -> str:
    return tool if tool in TOOLS_BY_NAME else "unknown"


def _metric_outcome(outcome: str | None) -> str:
    return outcome if outcome in SAFE_OUTCOMES else "error"
