# How to run the quality gates

This guide shows you how to run the same checks CI runs before you open a change. Run them from the
repository root after `uv sync --frozen --dev`.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest --cov --cov-report=term-missing
uv run python -m stram_quant_mcp.tools.export_schema --check
uv run python tools/check_docs.py
```

If `ruff format --check` fails, run `uv run ruff format .` and review the diff.

If the schema check reports drift, you changed a tool contract. Do not regenerate the file to make the
check pass. Follow the contract-change rules in [Engineering standards](../../ENGINEERING.md#4-mcp-tool-contract-rules).

If a legacy-baseline lint rule fails on a file you did not touch, leave it alone. New and changed code
must satisfy every rule; see
[Readability and structure](../../ENGINEERING.md#31-readability-and-structure-rules).

To scan for secrets the way CI does:

```bash
uv run detect-secrets scan --all-files \
  --exclude-files '(^|/)(\.git|\.venv|\.mypy_cache|\.pytest_cache|\.ruff_cache|\.grimp_cache|\.import_linter_cache|build|contracts|tests/fixtures|uv\.lock)(/|$)' \
  | uv run python tools/check_secret_scan.py
```

The full list of gates and why each exists is in
[CI quality gates](../../ENGINEERING.md#12-ci-quality-gates).
