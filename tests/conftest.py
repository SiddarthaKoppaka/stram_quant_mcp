from __future__ import annotations

from pathlib import Path

import pytest
from stram_data.data_interface import DataInterface

from stram_quant_mcp.tool_adapter import QuantToolAdapter

ROOT = Path(__file__).parent.parent
SYNTHETIC_LAKE = ROOT / "tests/fixtures/lake"


@pytest.fixture
def data_interface() -> DataInterface:
    return DataInterface(SYNTHETIC_LAKE)


@pytest.fixture
def tool_adapter(data_interface: DataInterface) -> QuantToolAdapter:
    return QuantToolAdapter(data_interface)
