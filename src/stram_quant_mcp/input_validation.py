"""Explain a Pydantic input or output validation failure without echoing any submitted value."""

from __future__ import annotations

from pydantic import ValidationError

MAX_ISSUES = 4
MAX_REASON_CHARS = 400


def describe_validation_error(error: ValidationError) -> str:
    """Name each offending field and the rule it broke; never include the submitted values.

    Pydantic messages for the closed models describe a rule ("Input should be ..."), not the value
    that was sent, and ``loc`` is a field path. Together they tell the caller exactly what to fix.
    """
    issues: list[str] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in item["loc"]) or "arguments"
        issues.append(f"{location}: {item['msg']}")
        if len(issues) == MAX_ISSUES:
            break
    extra = error.error_count() - len(issues)
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return ("; ".join(issues) + suffix)[:MAX_REASON_CHARS] or "The arguments are invalid."
