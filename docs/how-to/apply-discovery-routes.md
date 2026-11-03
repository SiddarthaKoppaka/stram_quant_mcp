# How to apply the OAuth discovery routes

This guide shows you how to publish the two OAuth discovery paths on the same HTTPS origin as the MCP
endpoint, so clients can follow the `401` challenge to the protected-resource metadata. Run it on the
host that runs Tailscale Serve. You need root and the installed `deploy/stram_quant_siddu.sh`.

1. Check the current state:

   ```bash
   sudo bash deploy/stram_quant_siddu.sh discovery-routes status
   ```

2. If a route is missing, add both after reviewing them:

   ```bash
   sudo STRAM_CONFIRM_DISCOVERY_ROUTES=yes bash deploy/stram_quant_siddu.sh discovery-routes apply
   ```

3. Confirm the metadata is served:

   ```bash
   curl -s https://siddu.tailc5da4c.ts.net/.well-known/oauth-protected-resource/mcp
   ```

   Expect JSON whose `resource` and `authorization_servers` match your configuration.

To roll back, run the same command as step 2 with `remove` instead of `apply`. Only the two
`/.well-known/` handlers are touched; the existing `/mcp` and `/realms` routes are not.

For why these paths are needed, see [About the architecture](../explanation/architecture.md). For the
exact routes, see the `discovery-routes` function in
[the installer](../../deploy/stram_quant_siddu.sh).
