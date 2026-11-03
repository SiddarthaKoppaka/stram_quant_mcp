# How to switch the server implementation

This guide shows you how to run the previous hand-wired server instead of the FastMCP server, for
example to roll back quickly. FastMCP is the default. The previous server is a temporary rollback aid
and is removed once the FastMCP release has passed staging; see
[Framework rules](../../ENGINEERING.md#34-framework-rules).

1. Set the variable in the service environment:

   ```bash
   STRAM_MCP_SERVER_IMPLEMENTATION=sdk
   ```

   Accepted values are `fastmcp` (default) and `sdk`. Any other value stops the service at startup.

2. Restart the service and confirm it is ready:

   ```bash
   curl --fail https://quant.example.invalid/health/ready
   ```

3. Confirm both servers behave the same by running the acceptance runner described in
   [Operate the service](operate-the-service.md).

Both servers use the same tool contracts, audit journal, worker and configuration, so no data or
configuration migration is needed in either direction. To roll back a whole release instead, restore the
previous image digest as described in the rollback section of [Operate the service](operate-the-service.md).

The `sdk` setting does not provide the denial throttle, hidden tools or the `retryable` error flag.
