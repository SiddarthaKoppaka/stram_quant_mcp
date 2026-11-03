from __future__ import annotations

import pytest

from stram_quant_mcp import app
from stram_quant_mcp.fastmcp_server import FastMcpApplication


def _stub_environment(monkeypatch: pytest.MonkeyPatch, implementation: str | None) -> list[str]:
    built: list[str] = []
    monkeypatch.setattr(app.AppSettings, "from_env", classmethod(lambda _cls: object()))
    monkeypatch.setattr(app, "configure_telemetry", lambda _settings: object())
    monkeypatch.setattr(
        app,
        "create_fastmcp_application",
        lambda *_args, **_kwargs: built.append("fastmcp") or FastMcpApplication("fast", None, None),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        app,
        "create_application",
        lambda *_args, **_kwargs: built.append("sdk") or type("Built", (), {"app": "sdk"})(),
    )
    if implementation is None:
        monkeypatch.delenv(app.IMPLEMENTATION_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(app.IMPLEMENTATION_VARIABLE, implementation)
    return built


def test_fastmcp_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _stub_environment(monkeypatch, None)

    assert app.create_from_env() == "fast"
    assert built == ["fastmcp"]


def test_the_previous_server_can_be_selected_for_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _stub_environment(monkeypatch, "sdk")

    assert app.create_from_env() == "sdk"
    assert built == ["sdk"]


def test_an_unknown_implementation_name_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _stub_environment(monkeypatch, "something-else")

    with pytest.raises(ValueError, match="fastmcp or sdk"):
        app.create_from_env()
    assert built == []
