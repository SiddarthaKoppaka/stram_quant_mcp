# Documentation

These pages follow the [Diátaxis](https://diataxis.fr/) framework: each page has exactly one purpose,
so you can find what you need by asking what you are trying to do.

| If you want to... | Read | Type |
|---|---|---|
| Learn by doing, starting from nothing | [Getting started](tutorials/getting-started.md) | Tutorial |
| Accomplish a specific task | the [how-to guides](#how-to-guides) | How-to |
| Look up a fact, schema, setting or command | the [reference](#reference) | Reference |
| Understand why the service is built this way | the [explanations](#explanation) | Explanation |

## Tutorials

- [Getting started](tutorials/getting-started.md): run the service end to end on your machine and make
  your first authenticated tool call.

## How-to guides

- [Set up and connect a client](how-to/set-up-and-connect-a-client.md)
- [Run the quality gates](how-to/run-the-quality-gates.md)
- [Add a tool](how-to/add-a-tool.md)
- [Provision the Qual MCP Keycloak clients](how-to/provision-qual-clients.md)
- [Apply the OAuth discovery routes](how-to/apply-discovery-routes.md)
- [Operate the service](how-to/operate-the-service.md): the full runbook for staging, recovery and rollback.

## Reference

- [Tools](reference/tools.md): contracts, scopes and errors for all ten tools.
- [Configuration](reference/configuration.md): every environment variable.
- [Security exceptions](reference/security-exceptions.md): reviewed, time-bounded exceptions.

## Explanation

- [About the architecture](explanation/architecture.md): boundaries, request lifecycle, failure handling.
- [About the FastMCP migration](explanation/about-the-fastmcp-migration.md): why and how the server
  framework is changing.

## Project records

These are neither learning nor reference material.

- [STRAM HQ handoff](STRAM_HQ_HANDOFF.md): the consolidated M6 handoff.
- [Plans](../PLANS.md): milestones, evidence and open decisions.
- [Engineering standards](../ENGINEERING.md): the normative rules every change follows.
- [Design specs](superpowers/specs/) and [implementation plans](superpowers/plans/2026-11-03-fastmcp-migration.md).
- [Deployment prerequisites](../deploy/README.md) and the [changelog](../CHANGELOG.md).

## Writing documentation

Follow [Documentation rules](../ENGINEERING.md#11-change-review-and-documentation-discipline). Keep one
purpose per page, link instead of repeating, and make every command copy-pasteable.
