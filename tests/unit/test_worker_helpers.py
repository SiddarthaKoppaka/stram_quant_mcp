from __future__ import annotations

import errno
import json
import socket
from pathlib import Path

import pytest

from stram_quant_mcp import worker as worker_module
from stram_quant_mcp.errors import ToolUnavailableError
from stram_quant_mcp.worker import (
    QueryWorker,
    _decode_object,
    _deny_network,
    _install_network_seccomp,
    _send_error,
    verify_native_network_isolation,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.payload = b""

    def send_bytes(self, payload: bytes) -> None:
        self.payload = payload


class _NativeFunction:
    def __init__(self, *results: int) -> None:
        self.results = list(results)
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *_args: object) -> int:
        return self.results.pop(0)


class _NativeLibrary:
    def __init__(self, *, prctl: tuple[int, ...] = (0, 0)) -> None:
        self.prctl = _NativeFunction(*prctl)
        self.socket = _NativeFunction(-1)
        self.syscall = _NativeFunction(-1)


def test_worker_rejects_unsafe_resource_limits() -> None:
    lake = Path("/read-only-lake")
    with pytest.raises(ValueError, match="invalid worker resource limits"):
        QueryWorker(lake, max_queued=-1)
    with pytest.raises(ValueError, match="invalid worker resource limits"):
        QueryWorker(lake, max_response_bytes=1023)
    with pytest.raises(ValueError, match="invalid worker resource limits"):
        QueryWorker(lake, memory_limit_mb=127)


def test_decode_object_accepts_only_json_objects() -> None:
    assert _decode_object(b'{"ready":true}') == {"ready": True}
    for payload in (b"not-json", b"[]", b'"scalar"', b"\xff"):
        with pytest.raises(ToolUnavailableError):
            _decode_object(payload)


def test_send_error_uses_the_bounded_ipc_envelope() -> None:
    connection = _RecordingConnection()
    _send_error(connection, "tool_input_error")  # type: ignore[arg-type]
    assert json.loads(connection.payload) == {"ok": False, "error": "tool_input_error"}


@pytest.mark.asyncio
async def test_closed_worker_rejects_new_calls() -> None:
    worker = QueryWorker(Path("/read-only-lake"))
    assert worker.pid is None
    assert worker.restart_count == 0
    assert worker.policy_version == "unknown"
    assert not worker.dependency_versions_match
    await worker.close()
    with pytest.raises(ToolUnavailableError):
        await worker.start()
    with pytest.raises(ToolUnavailableError):
        await worker.call("probe", {}, "request", 1.0)
    with pytest.raises(ToolUnavailableError):
        await worker.call("probe", {}, "x" * 129, 1.0)


def test_python_network_guard_blocks_socket_entry_points(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.socket, "socket", socket.socket)
    monkeypatch.setattr(worker_module.socket, "create_connection", socket.create_connection)
    _deny_network()
    with pytest.raises(OSError, match="network disabled"):
        worker_module.socket.socket()
    with pytest.raises(OSError, match="network disabled"):
        worker_module.socket.create_connection(("127.0.0.1", 1))


def test_seccomp_policy_installs_for_reviewed_linux_architecture(monkeypatch) -> None:
    native = _NativeLibrary()
    monkeypatch.setattr(worker_module.sys, "platform", "linux")
    monkeypatch.setattr(worker_module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(worker_module.ctypes, "CDLL", lambda *_args, **_kwargs: native)
    _install_network_seccomp()
    assert native.prctl.results == []


def test_seccomp_policy_fails_closed_for_unknown_architecture(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.sys, "platform", "linux")
    monkeypatch.setattr(worker_module.platform, "machine", lambda: "riscv64")
    with pytest.raises(RuntimeError, match="unsupported"):
        _install_network_seccomp()


@pytest.mark.parametrize(
    ("results", "message"),
    [((1,), "no-new-privileges"), ((0, 1), "seccomp installation")],
)
def test_seccomp_policy_fails_closed_when_kernel_rejects_filter(
    monkeypatch, results: tuple[int, ...], message: str
) -> None:
    native = _NativeLibrary(prctl=results)
    monkeypatch.setattr(worker_module.sys, "platform", "linux")
    monkeypatch.setattr(worker_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(worker_module.ctypes, "CDLL", lambda *_args, **_kwargs: native)
    with pytest.raises(OSError, match=message):
        _install_network_seccomp()


def test_native_network_probe_checks_socket_and_io_uring(monkeypatch) -> None:
    native = _NativeLibrary()
    monkeypatch.setattr(worker_module.sys, "platform", "linux")
    monkeypatch.setattr(worker_module, "_install_network_seccomp", lambda: None)
    monkeypatch.setattr(worker_module.ctypes, "CDLL", lambda *_args, **_kwargs: native)
    monkeypatch.setattr(worker_module.ctypes, "get_errno", lambda: errno.EPERM)
    verify_native_network_isolation()
    assert native.socket.results == []
    assert native.syscall.results == []
