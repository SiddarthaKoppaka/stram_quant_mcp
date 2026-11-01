"""Emit and verify a hermetic OpenTelemetry fixture without contacting an exporter."""

from __future__ import annotations

import json
import logging
from io import StringIO

from opentelemetry.propagate import extract
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from stram_quant_mcp.telemetry import JsonEventLogger, Telemetry, correlation_ids


class FixtureSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def main() -> None:
    canary = "CANARY_SECRET_SHOULD_NOT_EXPORT"
    stream = StringIO()
    logger = logging.Logger("otel-fixture")
    logger.addHandler(logging.StreamHandler(stream))
    span_exporter = FixtureSpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=(metric_reader,), shutdown_on_exit=False)
    telemetry = Telemetry(
        tracer_provider.get_tracer("fixture"),
        meter_provider.get_meter("fixture"),
        JsonEventLogger(logger),
    )
    incoming_trace = "0af7651916cd43dd8448eb211c80319c"  # pragma: allowlist secret
    parent = extract(
        {
            "traceparent": f"00-{incoming_trace}-b7ad6b7169203331-01",
            "baggage": f"secret={canary}",
        }
    )
    with telemetry.start_span("stram.quant.tool", context=parent):
        trace_id, span_id = correlation_ids()
        telemetry.tool_result("quant_get_series", "success", "strict", 0.01)
        telemetry.events.emit(
            "tool.completed",
            trace_id=trace_id,
            span_id=span_id,
            tool="quant_get_series",
            outcome="success",
            duration_ms=10,
        )

    metric_data = metric_reader.get_metrics_data()
    if metric_data is None or not span_exporter.spans:
        raise SystemExit("telemetry fixture was not emitted")
    serialized_metrics = repr(metric_data)
    serialized_spans = repr(span_exporter.spans)
    log_line = stream.getvalue().strip()
    if canary in log_line or canary in serialized_metrics or canary in serialized_spans:
        raise SystemExit("seeded secret leaked into telemetry")
    span = span_exporter.spans[0]
    if f"{span.context.trace_id:032x}" != incoming_trace:
        raise SystemExit("W3C traceparent was not propagated")
    output = {
        "status": "ok",
        "trace_id": incoming_trace,
        "span_count": len(span_exporter.spans),
        "metric_scope_count": sum(
            len(resource.scope_metrics) for resource in metric_data.resource_metrics
        ),
        "log_event": json.loads(log_line)["event"],
        "canary_redacted": True,
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
