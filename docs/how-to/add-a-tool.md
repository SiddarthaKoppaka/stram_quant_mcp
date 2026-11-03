# How to add a tool

This guide shows you how to add a read-only tool. Because FastMCP tools, their schemas and their
authorization are all generated from one catalogue, you declare the tool once; you do not write
validation, scope checks, audit or error handling.

Before you start, confirm the tool is a direct call to the pinned public Data Interface. This service
adds no SQL, calculations or write paths; see
[Engineering standards](../../ENGINEERING.md#4-mcp-tool-contract-rules).

1. Add the closed, immutable input model and the output envelope in
   [`tool_models.py`](../../src/stram_quant_mcp/tool_models.py). Reject unknown properties and bound
   every string, number and collection.

2. Map the tool to its Data Interface method in
   [`tool_adapter.py`](../../src/stram_quant_mcp/tool_adapter.py). Do not add logic beyond translating
   models.

3. Declare the tool in [`tool_registry.py`](../../src/stram_quant_mcp/tool_registry.py) by adding a
   `ToolSpec` with its name, description, single required scope, models and deadline.

4. Regenerate the reviewed contract and review the diff:

   ```bash
   uv run python -m stram_quant_mcp.tools.export_schema
   git diff contracts/tool_schemas.json
   ```

5. If the scope is new, add it to the Keycloak scopes your clients request and to the
   [tool reference](../reference/tools.md).

6. Run the quality gates. The parity suite then checks the new tool automatically: it must be listed
   with the published schema and hash, be read-only, deny callers who lack its scope, and be audited.

   ```bash
   uv run pytest tests/parity tests/integration -q
   ```

If the parity suite reports a schema mismatch, you changed a published contract. Follow the
contract-change rules before regenerating anything else.

See [Run the quality gates](run-the-quality-gates.md) for the full check list.
