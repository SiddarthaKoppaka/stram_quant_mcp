# Shared Keycloak integration and Quant PRM routing (design)

Approved 2026-11-02. Scope: additive only; Quant audience, clients and security controls unchanged.

1. App: publish `scopes_supported` (`quant:*`) in RFC 9728 metadata.
2. Routing: Tailscale Serve on :443 must also proxy
   `/.well-known/oauth-protected-resource/mcp` (Quant, 127.0.0.1:8800) and
   `/.well-known/oauth-authorization-server/realms/stram-staging` (Keycloak, 127.0.0.1:9080).
3. Keycloak: new scope `stram-qual-claims` (audience `https://siddu.tailc5da4c.ts.net:8443/mcp`,
   `tenant_id`), optional scopes `qual:discover|read|analytics|text`, clients `stram-qual-smoke`,
   `stram-qual-text`, `stram-qual-discovery-only`. `qual:text` is never default.
   Live provisioning via idempotent admin-API script; not run without authorization.
4. Claims: acceptance tooling asserts iss, aud, exp, iat, sub, tenant_id, azp/client_id, scope.
5. Tests: positive and negative authentication/authorization; audience disjointness.
6. Port 8443 is not activated until the other team confirms readiness.
