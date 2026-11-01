from __future__ import annotations

from tools import check_docs


def test_documentation_gate_passes() -> None:
    assert check_docs.main() == 0
