from __future__ import annotations

from pathlib import Path

import pytest

from stram_quant_mcp import entrypoint
from stram_quant_mcp.entrypoint import mount_is_read_only
from tests.conftest import SYNTHETIC_LAKE

MOUNTINFO = """\
35 24 0:31 / / ro,relatime - overlay overlay ro
40 35 8:1 /quant /lake ro,nosuid,nodev,noexec,relatime - ext4 /dev/sda1 ro
41 35 8:1 /audit /audit rw,nosuid,nodev,noexec,relatime - ext4 /dev/sda1 rw
42 35 8:1 /space /path\\040with\\040space ro,relatime - ext4 /dev/sda1 ro
"""


def test_preflight_mount_parser_uses_longest_explicit_mount() -> None:
    assert mount_is_read_only(Path("/"), MOUNTINFO)
    assert mount_is_read_only(Path("/lake/dataset"), MOUNTINFO)
    assert not mount_is_read_only(Path("/audit"), MOUNTINFO)
    assert mount_is_read_only(Path("/path with space"), MOUNTINFO)
    assert not mount_is_read_only(Path("/missing"), "")


def _mountinfo(tmp_path: Path, *, root: str = "ro", lake: str = "ro") -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(
        f"35 24 0:31 / / {root},relatime - overlay overlay {root}\n"
        f"40 35 8:1 /quant {SYNTHETIC_LAKE.resolve()} {lake},relatime - ext4 /dev/sda1 {lake}\n"
    )
    return path


def test_preflight_checks_real_public_interface(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("STRAM_MCP_PARQUET_ROOT", str(SYNTHETIC_LAKE))
    result = entrypoint.preflight(
        mountinfo_path=_mountinfo(tmp_path),
        effective_uid=10001,
    )
    assert result == 0
    assert '"status": "preflight_ok"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("uid", "root_mode", "lake_mode", "message"),
    [
        (0, "ro", "ro", "root"),
        (10001, "rw", "ro", "root filesystem"),
        (10001, "ro", "rw", "lake mount"),
    ],
)
def test_preflight_fails_closed_on_privilege_or_mounts(
    monkeypatch, tmp_path, uid: int, root_mode: str, lake_mode: str, message: str
) -> None:
    monkeypatch.setenv("STRAM_MCP_PARQUET_ROOT", str(SYNTHETIC_LAKE))
    with pytest.raises(RuntimeError, match=message):
        entrypoint.preflight(
            mountinfo_path=_mountinfo(tmp_path, root=root_mode, lake=lake_mode),
            effective_uid=uid,
        )


def test_preflight_rejects_missing_lake_and_version_drift(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STRAM_MCP_PARQUET_ROOT", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="unavailable"):
        entrypoint.preflight(mountinfo_path=_mountinfo(tmp_path), effective_uid=10001)

    monkeypatch.setenv("STRAM_MCP_PARQUET_ROOT", str(SYNTHETIC_LAKE))
    monkeypatch.setattr(entrypoint.stram_data, "__version__", "wrong")
    with pytest.raises(RuntimeError, match="release mismatch"):
        entrypoint.preflight(mountinfo_path=_mountinfo(tmp_path), effective_uid=10001)


@pytest.mark.asyncio
async def test_worker_interface_probe_uses_production_worker_boundary(monkeypatch, capsys) -> None:
    calls: list[str] = []

    class ProbeWorker:
        dependency_versions_match = True

        def __init__(self, _lake: Path) -> None:
            calls.append("init")

        async def start(self) -> None:
            calls.append("start")

        async def call(
            self,
            tool: str,
            _arguments: dict[str, object],
            request_id: str,
            _timeout: float,
        ) -> dict[str, object]:
            calls.extend((tool, request_id))
            return {"data": {"policies": []}}

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(entrypoint, "QueryWorker", ProbeWorker)
    await entrypoint.worker_interface_probe()
    assert calls == [
        "init",
        "start",
        "quant_describe_availability",
        "container-worker-probe",
        "close",
    ]
    assert '"status":"worker_interface_ok"' in capsys.readouterr().out
