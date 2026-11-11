# About the FastMCP migration

This page explains why the server framework changed, what changed, what deliberately did not, and how
the change was made safely. It records a decision; it is not a procedure. The step-by-step work is in
the [implementation plan](../superpowers/plans/2026-11-03-fastmcp-migration.md).

**Status:** the FastMCP server is implemented, is the default, and passes the same black-box
behavior suite the previous server passed. It has not yet run against the real Keycloak and lake in
staging. The previous server was removed; its last working state is the `archive/sdk-server` branch, from
which a rollback image can be built.

## Why change

The service is built directly on the lowest layer of the official MCP Python SDK. That gave full
control, and it also means the repository carries a lot of code that is not specific to quantitative
data: a hand-written tool catalogue, a hand-written JWT verifier, and hand-wired transport and
authorization. New contributors have to learn all of it before they can add one tool.

[FastMCP](https://gofastmcp.com/) is a widely used framework built on the same SDK. It provides tool
declaration by decorator, JWT verification, per-tool scope checks and middleware. Using it should make
adding or reading a tool a matter of one small function, and should shrink the code this team has to
maintain and review.

## What the change covers

| Concern | Today | After the migration |
|---|---|---|
| Declaring a tool | Entry in a hand-built registry plus adapter wiring | A decorated function that calls the adapter |
| Verifying the token | Custom `JWTTokenVerifier` | FastMCP's Keycloak provider with a strict verifier that adds the checks the stock one lacks |
| Per-tool scope | Custom policy layer | A default-deny policy store consulted on every call and for every listing, plus the existing invocation re-check |
| Errors | Fixed message only | Fixed message plus a `retryable` flag; access denial is never retryable |
| Repeated denials | Not limited | A bounded per-caller throttle blocks callers who keep being denied |
| Readiness | Worker and audit | Also requires the JWKS endpoint to have served a signing key |

## What does not change

These are the parts that exist because of this service's security and data promises, not because of the
framework. They stay as they are and are called from the new tool functions:

- the bounded Data Plane backend (formerly an isolated query worker) with its deadlines and response budgets;
- the HMAC-chained audit journal, including one terminal record per call;
- the exact issuer, audience, tenant and expiry checks;
- Host and Origin allowlists and the redaction rules for logs and telemetry;
- the eleven tool contracts: names, closed input and output schemas, scopes and schema hashes. A client
  must not be able to tell that the framework changed.

## Why a spike comes first

The framework's documentation does not confirm everything this service depends on, for example the
exact `WWW-Authenticate` challenge and the RFC 9728 metadata we serve today. A short, throwaway
experiment answers those questions with evidence before any production code moves. If the framework
cannot meet a check, the migration stops or that part stays custom.

## Alternatives considered

- **Stay as is, tidy the code.** Lowest risk, but it keeps the custom surface that makes onboarding hard.
- **Use LangChain's MCP support.** It is a client library for agents that call MCP servers, not a server
  framework, so it cannot replace this service. It may be used later by consumers.
- **Another server framework.** None offered the same combination of SDK compatibility and built-in
  authorization at the time of the decision.

## Risks

The framework releases often and has changed behavior between patch versions, so one exact version is
pinned and upgraded only as an isolated change. It also adds transitive dependencies that must pass the
license and vulnerability gates.

For the rules new code must follow, see [Engineering standards](../../ENGINEERING.md#34-framework-rules).
