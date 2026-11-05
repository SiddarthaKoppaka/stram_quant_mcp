from __future__ import annotations

from pathlib import Path

import pytest

from stram_quant_mcp import entrypoint
from stram_quant_mcp.entrypoint import mount_is_read_only
from tests.support.fake_data_plane import FakeDataPlane

MOUNTINFO = """\
35 24 0:31 / / ro,relatime - overlay overlay ro
41 35 8:1 /audit /audit rw,nosuid,nodev,noexec,relatime - ext4 /dev/sda1 rw
42 35 8:1 /space /path\\040with\\040space ro,relatime - ext4 /dev/sda1 ro
"""


def test_preflight_mount_parser_uses_longest_explicit_mount() -> None:
    assert mount_is_read_only(Path("/"), MOUNTINFO)
    assert not mount_is_read_only(Path("/audit"), MOUNTINFO)
    assert mount_is_read_only(Path("/path with space"), MOUNTINFO)
    assert not mount_is_read_only(Path("/missing"), "")


def _mountinfo(tmp_path: Path, *, root: str = "ro") -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(f"35 24 0:31 / / {root},relatime - overlay overlay {root}\n")
    return path


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, secret: bool = True) -> None:
    secret_path = tmp_path / "data.secret"
    if secret:
        secret_path.write_text("test-client-secret")
    values = {
        "STRAM_MCP_DATA_URL": "http://testserver",
        "STRAM_MCP_DATA_TOKEN_URL": "http://testserver/token",
        "STRAM_MCP_DATA_CLIENT_ID": "stram-quant-mcp",
        "STRAM_MCP_DATA_CLIENT_SECRET_FILE": str(secret_path),
        "STRAM_MCP_AUDIT_DB": str(tmp_path / "audit.db"),
        "STRAM_MCP_AUDIT_KEY_FILE": str(tmp_path / "audit.key"),
        "STRAM_MCP_ISSUER_URL": "http://testserver/issuer",
        "STRAM_MCP_RESOURCE_URL": "http://testserver/mcp",
        "STRAM_MCP_JWKS_URL": "http://testserver/jwks",
        "STRAM_MCP_TENANT_ID": "stram",
        "STRAM_MCP_ALLOWED_HOSTS": "testserver",
        "STRAM_MCP_ALLOW_INSECURE_HTTP": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_preflight_passes_without_contacting_any_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, tmp_path)

    result = entrypoint.preflight(mountinfo_path=_mountinfo(tmp_path), effective_uid=10001)

    assert result == 0
    assert '"status": "preflight_ok"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("uid", "root_mode", "message"),
    [(0, "ro", "root"), (10001, "rw", "root filesystem")],
)
def test_preflight_fails_closed_on_privilege_or_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uid: int, root_mode: str, message: str
) -> None:
    _configure(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match=message):
        entrypoint.preflight(mountinfo_path=_mountinfo(tmp_path, root=root_mode), effective_uid=uid)


def test_preflight_rejects_a_missing_client_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, secret=False)
    with pytest.raises(RuntimeError, match="secret file"):
        entrypoint.preflight(mountinfo_path=_mountinfo(tmp_path), effective_uid=10001)


@pytest.mark.asyncio
async def test_data_plane_probe_starts_the_real_backend_and_reads_policies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    plane = FakeDataPlane()
    original = entrypoint.DataPlaneClient

    def client_over_fake(settings: object) -> object:
        return original(settings, transport=plane)  # type: ignore[arg-type]

    monkeypatch.setattr(entrypoint, "DataPlaneClient", client_over_fake)

    await entrypoint.data_plane_probe()

    assert '"status":"data_plane_ok"' in capsys.readouterr().out
    assert plane.calls_to("/v1/policies")
