"""Every tool must describe its purpose, bounds and failures in the same reviewed shape."""

from __future__ import annotations

import pytest

from stram_quant_mcp.tool_docs import TOOL_DOCS
from stram_quant_mcp.tool_registry import TOOL_SPECS

SECTIONS = ("Use when:", "Do not use for:", "Inputs:", "Returns:", "Read-only and idempotent")
FAILURE_CODES = (
    "invalid_request",
    "access_denied",
    "unavailable",
    "overloaded",
    "deadline_exceeded",
    "data_plane_error",
    "service_credentials_rejected",
)


def test_every_registered_tool_has_documentation_and_no_extra_entries() -> None:
    assert set(TOOL_DOCS) == {spec.name for spec in TOOL_SPECS}


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_description_has_every_section_and_every_failure_category(spec) -> None:  # type: ignore[no-untyped-def]
    for section in SECTIONS:
        assert section in spec.description, (spec.name, section)
    for code in FAILURE_CODES:
        assert code in spec.description, (spec.name, code)
    assert "retry_after_seconds" in spec.description


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_description_is_plain_ascii_and_bounded(spec) -> None:  # type: ignore[no-untyped-def]
    assert spec.description.isascii()
    assert len(spec.description) < 2500


def test_tools_that_can_miss_or_overflow_say_so() -> None:
    lookups = ("quant_list_indicator_subjects", "quant_get_lineage")
    bulk = ("quant_get_series", "quant_get_derived", "quant_get_aligned_panel")
    docs = {spec.name: spec.description for spec in TOOL_SPECS}

    assert all("- not_found:" in docs[name] for name in lookups)
    assert all("- resource_limit:" in docs[name] for name in bulk)
    assert "- not_found:" not in docs["quant_get_freshness"]
