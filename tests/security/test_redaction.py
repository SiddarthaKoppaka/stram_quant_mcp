from __future__ import annotations

import logging
from io import StringIO

from opentelemetry import metrics, trace

from stram_quant_mcp.telemetry import JsonEventLogger, Telemetry


def test_untrusted_values_cannot_become_log_or_metric_labels() -> None:
    canary = "CANARY_SECRET_bearer_path_series_result"
    stream = StringIO()
    logger = logging.Logger("redaction-test")
    logger.addHandler(logging.StreamHandler(stream))
    telemetry = Telemetry(
        trace.get_tracer("redaction-test"),
        metrics.get_meter("redaction-test"),
        JsonEventLogger(logger),
    )

    telemetry.events.emit("tool.completed", tool=canary, outcome=canary)
    telemetry.tool_result(canary, canary, canary, 0.1)

    rendered = stream.getvalue()
    assert canary not in rendered
    assert '"tool": "unknown"' in rendered or '"tool":"unknown"' in rendered
    assert '"outcome": "error"' in rendered or '"outcome":"error"' in rendered
