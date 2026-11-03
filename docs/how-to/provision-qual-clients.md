# How to provision the Qual MCP Keycloak clients

This guide shows you how to create, verify and repair the Keycloak scopes and clients used by the
second (Qual) MCP in the shared `stram-staging` realm. The tool only creates or reconciles
`stram-qual-*` clients and `qual:*` scopes. It never reads or changes the Quant clients, and it never
prints secrets or tokens. Run it as root on the Keycloak host, because it needs the bootstrap password.

1. Plan the change without writing anything:

   ```bash
   sudo .venv/bin/python tools/provision_qual_clients.py --dry-run \
     --public-url https://siddu.tailc5da4c.ts.net --realm stram-staging \
     --qual-resource https://siddu.tailc5da4c.ts.net:8443/mcp \
     --quant-resource https://siddu.tailc5da4c.ts.net/mcp --tenant local-stram \
     --bootstrap-password-file /etc/stram-quant/secrets/keycloak-bootstrap-password \
     --secret-dir /etc/stram-quant/secrets/qual
   ```

2. If the plan contains only `stram-qual-*` and `qual:*` changes, run the same command with `--verify`
   instead of `--dry-run`. It reports `PASS` when the claims, audience isolation and scope policy hold.

3. Hand each needed client secret to its owner through your secret channel. Never use chat or Git. Give
   `stram-qual-text` only to a service that needs `qual:text`.

If it reports that a `qual:*` scope is a realm default, remove it in the Keycloak admin console and run
again; the tool refuses to continue until the default is gone.

For the scopes each client may request, see [Qual client policy](../reference/configuration.md#qual-client-policy).
