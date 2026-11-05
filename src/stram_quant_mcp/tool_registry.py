"""Reviewed V1 tool definitions and their reproducible schema hashes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel

from stram_quant_mcp.tool_docs import TOOL_DOCS
from stram_quant_mcp.tool_models import (
    AvailabilityPoliciesEnvelope,
    CatalogEnvelope,
    CatalogInput,
    ConditionalReturnsEnvelope,
    ConditionalReturnsInput,
    DerivedEnvelope,
    DerivedInput,
    EmptyInput,
    FreshnessEnvelope,
    IndicatorsEnvelope,
    IndicatorSubjectsEnvelope,
    IndicatorSubjectsInput,
    LineageEnvelope,
    LineageInput,
    PanelEnvelope,
    PanelInput,
    SeriesEnvelope,
    SeriesInput,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One direct, read-only Data Interface operation."""

    name: str
    description: str
    scope: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_seconds: float

    def definition(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "inputSchema": self.input_model.model_json_schema(),
            "outputSchema": self.output_model.model_json_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }

    def schema_hash(self) -> str:
        encoded = json.dumps(self.definition(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


TOOL_SPECS = (
    ToolSpec(
        "quant_list_series",
        TOOL_DOCS["quant_list_series"].render(),
        "quant:discover",
        CatalogInput,
        CatalogEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_list_indicators",
        TOOL_DOCS["quant_list_indicators"].render(),
        "quant:discover",
        EmptyInput,
        IndicatorsEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_list_indicator_subjects",
        TOOL_DOCS["quant_list_indicator_subjects"].render(),
        "quant:discover",
        IndicatorSubjectsInput,
        IndicatorSubjectsEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_describe_availability",
        TOOL_DOCS["quant_describe_availability"].render(),
        "quant:discover",
        EmptyInput,
        AvailabilityPoliciesEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_get_freshness",
        TOOL_DOCS["quant_get_freshness"].render(),
        "quant:discover",
        EmptyInput,
        FreshnessEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_get_series",
        TOOL_DOCS["quant_get_series"].render(),
        "quant:read",
        SeriesInput,
        SeriesEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_get_derived",
        TOOL_DOCS["quant_get_derived"].render(),
        "quant:read",
        DerivedInput,
        DerivedEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_get_lineage",
        TOOL_DOCS["quant_get_lineage"].render(),
        "quant:lineage",
        LineageInput,
        LineageEnvelope,
        10.0,
    ),
    ToolSpec(
        "quant_get_aligned_panel",
        TOOL_DOCS["quant_get_aligned_panel"].render(),
        "quant:analytics",
        PanelInput,
        PanelEnvelope,
        20.0,
    ),
    ToolSpec(
        "quant_get_conditional_returns",
        TOOL_DOCS["quant_get_conditional_returns"].render(),
        "quant:analytics",
        ConditionalReturnsInput,
        ConditionalReturnsEnvelope,
        20.0,
    ),
)

TOOLS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def exported_definitions() -> dict[str, object]:
    """Return stable definitions and hashes for review and drift detection."""

    return {
        "tools": [{**spec.definition(), "schemaHash": spec.schema_hash()} for spec in TOOL_SPECS]
    }
