"""The routes and scopes this service relies on must exist in the installed Data Plane app."""

from __future__ import annotations

import json
from pathlib import Path

from stram_data.data_interface import DataInterface
from stram_data.service.app import create_app

from tests.support.fake_data_plane import LAKE

ROOT = Path(__file__).parents[2]
SNAPSHOT = json.loads((ROOT / "contracts/data_service.v1.9.0.json").read_text())


class _Verifier:
    def verify(self, token: str) -> None:
        raise AssertionError(token)


def _schema() -> dict[str, dict[str, dict[str, object]]]:
    app = create_app(DataInterface(LAKE), _Verifier())  # type: ignore[arg-type]
    paths: dict[str, dict[str, dict[str, object]]] = app.openapi()["paths"]
    return paths


def test_every_route_we_call_exists_with_the_scope_we_request() -> None:
    schema = _schema()
    for route in SNAPSHOT["routes"]:
        operation = schema[route["path"]][route["method"].lower()]
        assert operation["x-required-scope"] == route["scope"], route


def test_optional_scopes_are_only_the_hindsight_scope() -> None:
    assert SNAPSHOT["optional_scopes"] == ["hindsight:read"]
