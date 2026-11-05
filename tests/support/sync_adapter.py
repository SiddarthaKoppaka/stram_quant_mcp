"""Run the async tool adapter from synchronous tests."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from stram_quant_mcp.tool_adapter import QuantToolAdapter

CALL_DEADLINE_SECONDS = 20.0


class SyncAdapter:
    """Blocking facade over ``QuantToolAdapter`` with the same ``execute`` signature."""

    def __init__(self, adapter: QuantToolAdapter) -> None:
        self._adapter = adapter

    def execute(self, tool_name: str, arguments: object, request_id: str) -> BaseModel:
        """Run one tool call to completion on a private event loop."""

        return asyncio.run(
            self._adapter.execute(tool_name, arguments, request_id, CALL_DEADLINE_SECONDS)
        )
