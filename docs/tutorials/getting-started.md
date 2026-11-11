# Getting started

In this tutorial you will install the project, start the real MCP server on your machine with a
throwaway identity, and watch an MCP client discover the eleven tools and read a record from a small
synthetic dataset. Nothing here touches production data, credentials or the network beyond loopback.

You need CPython 3.12 or newer, `uv`, and Git. Read access to the pinned `stram_data` source is needed
only if it is not already cached.

## 1. Install the dependencies

From the repository root:

```bash
uv sync --frozen --dev
```

You should see `uv` resolve and install packages without errors.

## 2. Check the pinned data interface

```bash
uv run python -c 'import stram_data; print(stram_data.__version__)'
```

You should see `1.9.0`. At startup the server also checks the Data Plane service it connects to and refuses to start against an incompatible version.

## 3. Run the end-to-end test

```bash
uv run pytest tests/integration/test_mcp_client.py -q
```

You should see a single dot and `1 passed`.

That one test did the whole job: it generated a temporary signing key, signed a five-minute token,
started the authenticated server on a free loopback port, connected with the official MCP client,
listed the eleven tools, called `quant_list_series`, checked the record it got back, and shut everything
down.

## 4. Look at what the client received

Open [the test](../../tests/integration/test_fastmcp_client.py) and find the call to `list_tools`. Each tool
you listed is described in the [tool reference](../reference/tools.md), with the scope it needs.

You now have a working local server, and you have seen the full path a request takes: token check,
scope check, bounded Data Plane backend, Data Plane service.

## Where to go next

- To connect your own client: [Set up and connect a client](../how-to/set-up-and-connect-a-client.md).
- To understand the path you just saw: [About the architecture](../explanation/architecture.md).
- To change the code: [Run the quality gates](../how-to/run-the-quality-gates.md).
