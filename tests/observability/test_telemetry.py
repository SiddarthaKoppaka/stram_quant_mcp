from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest
from opentelemetry.propagate import extract
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from stram_quant_mcp.settings import AppSettings
from stram_quant_mcp.telemetry import (
    JsonEventLogger,
    Telemetry,
    configure_telemetry,
    correlation_ids,
)


class MemorySpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def _telemetry(stream: StringIO):
    span_exporter = MemorySpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=(metric_reader,), shutdown_on_exit=False)
    logger = logging.Logger("telemetry-test")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    telemetry = Telemetry(
        tracer_provider.get_tracer("test"),
        meter_provider.get_meter("test"),
        JsonEventLogger(logger),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    return telemetry, span_exporter, metric_reader


def test_trace_log_correlation_and_metric_labels_are_bounded() -> None:
    stream = StringIO()
    telemetry, span_exporter, metric_reader = _telemetry(stream)

    with telemetry.start_span("test.tool"):
        trace_id, span_id = correlation_ids()
        telemetry.events.emit(
            "tool.completed",
            trace_id=trace_id,
            span_id=span_id,
            request_id="request-1",
            tool="quant_get_series",
            outcome="success",
            duration_ms=12.5,
        )
        telemetry.tool_result("quant_get_series", "success", "strict", 0.0125)

    event = json.loads(stream.getvalue())
    assert event["trace_id"] == f"{span_exporter.spans[0].context.trace_id:032x}"
    assert event["span_id"] == f"{span_exporter.spans[0].context.span_id:016x}"
    metrics = metric_reader.get_metrics_data()
    assert metrics is not None
    points = [
        point
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
    ]
    assert points
    assert all("request_id" not in point.attributes for point in points)
    assert all("series" not in point.attributes for point in points)


def test_w3c_traceparent_is_preserved_as_parent_context() -> None:
    stream = StringIO()
    telemetry, span_exporter, _metric_reader = _telemetry(stream)
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    parent_span_id = "b7ad6b7169203331"
    parent = extract({"traceparent": f"00-{trace_id}-{parent_span_id}-01"})

    with telemetry.start_span("http.server.request", context=parent):
        pass

    span = span_exporter.spans[0]
    assert f"{span.context.trace_id:032x}" == trace_id
    assert span.parent is not None
    assert f"{span.parent.span_id:016x}" == parent_span_id


@pytest.mark.asyncio
async def test_production_telemetry_configuration_without_exporter(tmp_path: Path, capsys) -> None:
    settings = AppSettings(
        parquet_root=tmp_path,
        audit_db_path=tmp_path / "audit.db",
        audit_key_path=tmp_path / "audit.key",
        issuer_url="https://issuer.example",
        resource_url="https://quant.example/mcp",
        jwks_url="https://issuer.example/jwks",
        tenant_id="stram",
        allowed_hosts=("quant.example",),
    )
    telemetry = configure_telemetry(settings)
    with telemetry.start_span("configured"):
        trace_id, span_id = correlation_ids()
        telemetry.events.emit("configured", trace_id=trace_id, span_id=span_id)
    telemetry.set_ready(True)
    telemetry.set_ready(False)
    await telemetry.shutdown()
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "configured"
    assert len(event["trace_id"]) == 32
