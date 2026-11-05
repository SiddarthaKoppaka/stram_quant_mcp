"""Container command boundary with a side-effect-free deployment preflight."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from stram_data.data_interface import INTERFACE_VERSION

from stram_quant_mcp.app import main as serve
from stram_quant_mcp.data_client import DataPlaneClient, DataPlaneSettings
from stram_quant_mcp.http_backend import HttpDataBackend
from stram_quant_mcp.settings import AppSettings


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
    """Verify the immutable runtime and configuration without contacting any service."""

    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid == 0:
        raise RuntimeError("container must not run as root")
    if not mountinfo_path.is_file():
        raise RuntimeError("Linux mount metadata is unavailable")
    if not mount_is_read_only(Path("/"), mountinfo_path.read_text()):
        raise RuntimeError("container root filesystem is not read-only")
    settings = AppSettings.from_env()
    if not settings.data_client_secret_path.is_file():
        raise RuntimeError("Data Plane client secret file is unavailable")
    result = {
        "status": "preflight_ok",
        "interface_version": INTERFACE_VERSION,
        "data_plane": "http",
        "root_read_only": True,
        "uid": uid,
    }
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


async def data_plane_probe() -> None:
    """Start the production backend and read the real Data Plane's availability policies."""

    settings = AppSettings.from_env()
    client = DataPlaneClient(
        DataPlaneSettings(
            base_url=settings.data_url,
            token_url=settings.data_token_url,
            client_id=settings.data_client_id,
            client_secret_path=settings.data_client_secret_path,
            connect_timeout_seconds=settings.data_connect_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
            allow_insecure_http=settings.allow_insecure_http,
        )
    )
    backend = HttpDataBackend(client)
    try:
        await backend.start()
        result = await backend.call("quant_describe_availability", {}, "container-probe", 10.0)
        if not backend.dependency_versions_match or not isinstance(result.get("data"), dict):
            raise RuntimeError("Data Plane probe failed")
    finally:
        await backend.close()
    sys.stdout.write('{"status":"data_plane_ok"}\n')


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve" and len(sys.argv) == 2:
        preflight()
        serve()
        return
    if command == "preflight" and len(sys.argv) == 2:
        raise SystemExit(preflight())
    if command == "data-plane-probe" and len(sys.argv) == 2:
        asyncio.run(data_plane_probe())
        return
    raise SystemExit("usage: stram_quant_mcp.entrypoint [serve|preflight|data-plane-probe]")


def _unescape_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


if __name__ == "__main__":
    main()
