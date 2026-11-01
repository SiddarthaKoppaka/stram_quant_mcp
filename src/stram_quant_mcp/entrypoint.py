"""Container command boundary with a side-effect-free deployment preflight."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import stram_data
from stram_data.data_interface import INTERFACE_VERSION, DataInterface

from stram_quant_mcp.app import main as serve
from stram_quant_mcp.worker import (
    EXPECTED_INTERFACE_VERSION,
    EXPECTED_STRAM_DATA_VERSION,
    QueryWorker,
    verify_native_network_isolation,
)


def mount_is_read_only(target: Path, mountinfo: str) -> bool:
    """Return the longest matching Linux mount's explicit `ro` state."""

    resolved = target.resolve()
    matches: list[tuple[int, set[str]]] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            continue
        mount_point = Path(_unescape_mount(fields[4])).resolve()
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        matches.append((len(mount_point.parts), set(fields[5].split(","))))
    if not matches:
        return False
    _depth, options = max(matches, key=lambda item: item[0])
    return "ro" in options and "rw" not in options


def preflight(
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    effective_uid: int | None = None,
) -> int:
    """Verify the immutable runtime, read-only mounts, and public Data Interface."""

    lake = Path(os.environ.get("STRAM_MCP_PARQUET_ROOT", "/lake"))
    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid == 0:
        raise RuntimeError("container must not run as root")
    if not lake.is_dir() or not mountinfo_path.is_file():
        raise RuntimeError("lake or Linux mount metadata is unavailable")
    mountinfo = mountinfo_path.read_text()
    if not mount_is_read_only(Path("/"), mountinfo):
        raise RuntimeError("container root filesystem is not read-only")
    if not mount_is_read_only(lake, mountinfo):
        raise RuntimeError("quantitative lake mount is not read-only")
    if stram_data.__version__ != EXPECTED_STRAM_DATA_VERSION:
        raise RuntimeError("stram-data release mismatch")
    if INTERFACE_VERSION != EXPECTED_INTERFACE_VERSION:
        raise RuntimeError("Data Interface version mismatch")
    data = DataInterface(lake)
    policies = data.describe_availability_policies()
    if not policies:
        raise RuntimeError("availability policies are unavailable")
    result = {
        "status": "preflight_ok",
        "interface_version": INTERFACE_VERSION,
        "stram_data_version": stram_data.__version__,
        "lake_read_only": True,
        "root_read_only": True,
        "uid": uid,
    }
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


async def worker_interface_probe() -> None:
    """Start the production worker boundary and call the real public Data Interface."""

    lake = Path(os.environ.get("STRAM_MCP_PARQUET_ROOT", "/lake"))
    worker = QueryWorker(lake)
    try:
        await worker.start()
        result = await worker.call(
            "quant_describe_availability",
            {},
            "container-worker-probe",
            10.0,
        )
        if not worker.dependency_versions_match or not isinstance(result.get("data"), dict):
            raise RuntimeError("worker Data Interface probe failed")
    finally:
        await worker.close()
    sys.stdout.write('{"status":"worker_interface_ok"}\n')


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve" and len(sys.argv) == 2:
        preflight()
        serve()
        return
    if command == "preflight" and len(sys.argv) == 2:
        raise SystemExit(preflight())
    if command == "worker-network-probe" and len(sys.argv) == 2:
        verify_native_network_isolation()
        sys.stdout.write('{"status":"worker_network_isolated"}\n')
        return
    if command == "worker-interface-probe" and len(sys.argv) == 2:
        asyncio.run(worker_interface_probe())
        return
    raise SystemExit(
        "usage: stram_quant_mcp.entrypoint "
        "[serve|preflight|worker-network-probe|worker-interface-probe]"
    )


def _unescape_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


if __name__ == "__main__":
    main()
