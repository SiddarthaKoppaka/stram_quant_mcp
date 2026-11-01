from stram_quant_mcp import __version__


def test_service_version_is_independent() -> None:
    assert __version__ == "0.1.0"
