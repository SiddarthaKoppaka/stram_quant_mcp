"""Bounded JSON IPC and lifecycle isolation for blocking Data Interface calls."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import json
import multiprocessing
import os
import platform
import socket
import sys
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

from pydantic import JsonValue

from stram_quant_mcp.errors import (
    QuantToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolOverloadedError,
    ToolResourceLimitError,
    ToolTimeoutError,
    ToolUnavailableError,
)

DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MEMORY_LIMIT_MB = 1024
EXPECTED_STRAM_DATA_VERSION = "1.3.1"
EXPECTED_INTERFACE_VERSION = "1"
WorkerTarget = Callable[[Connection, str, int, int], None]

_NETWORK_SYSCALLS: dict[str, tuple[int, tuple[int, ...]]] = {
    # audit architecture, socket family, io_uring setup/submit/register. Denying io_uring prevents
    # native code from submitting socket/connect operations without the corresponding syscall.
    "x86_64": (
        0xC000003E,
        (41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 288, 299, 307, 425, 426, 427),
    ),
    "aarch64": (
        0xC00000B7,
        (
            198,
            199,
            200,
            201,
            202,
            203,
            204,
            205,
            206,
            207,
            208,
            209,
            210,
            211,
            212,
            242,
            243,
            269,
            425,
            426,
            427,
        ),
    ),
}


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class QueryWorker:
    """Own one disposable query process with bounded admission and hard deadlines."""

    def __init__(
        self,
        parquet_root: str | Path,
        *,
        queue_wait_seconds: float = 2.0,
        max_queued: int = 2,
        startup_timeout_seconds: float = 10.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        max_restarts: int = 3,
        restart_backoff_seconds: float = 0.25,
        process_target: WorkerTarget | None = None,
    ) -> None:
        if (
            max_queued < 0
            or max_response_bytes < 1024
            or memory_limit_mb < 128
            or max_restarts < 0
            or restart_backoff_seconds < 0
        ):
            raise ValueError("invalid worker resource limits")
        self._parquet_root = str(parquet_root)
        self._queue_wait_seconds = queue_wait_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._memory_limit_mb = memory_limit_mb
        self._max_restarts = max_restarts
        self._restart_backoff_seconds = restart_backoff_seconds
        self._target = process_target or query_worker_main
        self._context = multiprocessing.get_context("spawn")
        self._slots = asyncio.BoundedSemaphore(1 + max_queued)
        self._execution_lock = asyncio.Lock()
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._closing = False
        self._restart_count = 0
        self._consecutive_failures = 0
        self._restart_exhausted = False
        self._policy_version = "unknown"
        self._stram_data_version = "unknown"
        self._interface_version = "unknown"

    @property
    def ready(self) -> bool:
        return bool(self._process and self._process.is_alive() and not self._closing)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def dependency_versions_match(self) -> bool:
        return (
            self._stram_data_version == EXPECTED_STRAM_DATA_VERSION
            and self._interface_version == EXPECTED_INTERFACE_VERSION
        )

    async def start(self) -> None:
        async with self._execution_lock:
            if self._closing:
                raise ToolUnavailableError from None
            await asyncio.to_thread(self._ensure_started)

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        timeout_seconds: float,
    ) -> dict[str, JsonValue]:
        if self._closing or timeout_seconds <= 0 or len(request_id) > 128:
            raise ToolUnavailableError from None
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self._queue_wait_seconds)
        except TimeoutError as error:
            raise ToolOverloadedError from error
        try:
            async with self._execution_lock:
                if self._closing:
                    raise ToolUnavailableError from None
                operation = asyncio.create_task(
                    asyncio.to_thread(
                        self._execute_blocking,
                        tool_name,
                        arguments,
                        request_id,
                        timeout_seconds,
                    )
                )
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # The subprocess still has a hard deadline. Wait for bounded cleanup so no
                    # background thread or query survives the cancelled caller.
                    await operation
                    raise
        finally:
            self._slots.release()

    async def close(self, drain_seconds: float = 5.0) -> None:
        self._closing = True
        try:
            async with asyncio.timeout(drain_seconds):
                async with self._execution_lock:
                    await asyncio.to_thread(self._stop_process, True)
        except TimeoutError:
            await asyncio.to_thread(self._stop_process, False)

    def _execute_blocking(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        request_id: str,
        timeout_seconds: float,
    ) -> dict[str, JsonValue]:
        self._ensure_started()
        connection = self._connection
        if connection is None:
            raise ToolUnavailableError from None
        message = json.dumps(
            {"tool": tool_name, "arguments": arguments, "request_id": request_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            connection.send_bytes(message)
            if not connection.poll(timeout_seconds):
                self._restart_process()
                raise ToolTimeoutError from None
            raw = connection.recv_bytes(self._max_response_bytes + 1)
        except (BrokenPipeError, EOFError, OSError):
            self._restart_process()
            raise ToolUnavailableError from None
        if len(raw) > self._max_response_bytes:
            self._restart_process()
            raise ToolResourceLimitError from None
        response = _decode_object(raw)
        if response.get("ok") is True:
            self._consecutive_failures = 0
            payload = response.get("payload")
            if isinstance(payload, dict):
                return payload
            raise ToolUnavailableError from None
        self._consecutive_failures = 0
        error_type = _ERROR_TYPES.get(response.get("error"), ToolUnavailableError)
        raise error_type from None

    def _ensure_started(self) -> None:
        if self._closing or self._restart_exhausted:
            raise ToolUnavailableError from None
        if self._process and self._process.is_alive() and self._connection:
            return
        self._spawn_process()

    def _spawn_process(self) -> None:
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self._target,
            args=(
                child,
                self._parquet_root,
                self._max_response_bytes,
                self._memory_limit_mb,
            ),
            name="stram-quant-query",
            daemon=True,
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        if not parent.poll(self._startup_timeout_seconds):
            self._stop_process(False)
            raise ToolUnavailableError from None
        try:
            ready = _decode_object(parent.recv_bytes(4096))
        except (EOFError, OSError):
            self._stop_process(False)
            raise ToolUnavailableError from None
        if ready.get("ready") is not True:
            self._stop_process(False)
            raise ToolUnavailableError from None
        policy_version = ready.get("policy_version")
        self._policy_version = policy_version if isinstance(policy_version, str) else "unknown"
        stram_data_version = ready.get("stram_data_version")
        self._stram_data_version = (
            stram_data_version if isinstance(stram_data_version, str) else "unknown"
        )
        interface_version = ready.get("interface_version")
        self._interface_version = (
            interface_version if isinstance(interface_version, str) else "unknown"
        )

    def _restart_process(self) -> None:
        self._stop_process(False)
        if self._closing:
            return
        self._restart_count += 1
        self._consecutive_failures += 1
        if self._consecutive_failures > self._max_restarts:
            self._restart_exhausted = True
            return
        if self._restart_backoff_seconds:
            time.sleep(self._restart_backoff_seconds)
        self._spawn_process()

    def _stop_process(self, graceful: bool) -> None:
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        if process is None:
            if connection:
                connection.close()
            return
        if graceful and process.is_alive() and connection:
            try:
                connection.send_bytes(b'{"control":"shutdown"}')
                process.join(timeout=2.0)
            except (BrokenPipeError, OSError):
                pass
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)
        if connection:
            connection.close()
        process.close()


_ERROR_TYPES: dict[object, type[QuantToolError]] = {
    ToolInputError.code: ToolInputError,
    ToolNotFoundError.code: ToolNotFoundError,
    ToolUnavailableError.code: ToolUnavailableError,
    ToolTimeoutError.code: ToolTimeoutError,
    ToolOverloadedError.code: ToolOverloadedError,
    ToolResourceLimitError.code: ToolResourceLimitError,
}


def _decode_object(raw: bytes) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ToolUnavailableError from error
    if not isinstance(value, dict):
        raise ToolUnavailableError from None
    return value


def _deny_network() -> None:
    """Block Python socket entry points in addition to the kernel seccomp filter."""

    def blocked(*_args: object, **_kwargs: object) -> object:
        raise OSError("network disabled in query worker")

    # These process-local replacements deliberately violate the socket module's static types.
    socket.socket = blocked  # type: ignore[assignment,misc]
    socket.create_connection = blocked  # type: ignore[assignment]


def _install_network_seccomp() -> None:
    """Fail closed after installing a Linux BPF filter for network and io_uring syscalls."""

    if not sys.platform.startswith("linux"):
        return
    machine = platform.machine().lower()
    syscall_policy = _NETWORK_SYSCALLS.get(machine)
    if syscall_policy is None:
        raise RuntimeError("worker network seccomp is unsupported on this architecture")
    audit_architecture, syscall_numbers = syscall_policy

    # Refuse an unexpected syscall ABI, then deny the reviewed network syscalls with EPERM.
    instructions = [
        _SockFilter(0x20, 0, 0, 4),  # BPF_LD | BPF_W | BPF_ABS: seccomp_data.arch
        _SockFilter(0x15, 1, 0, audit_architecture),
        _SockFilter(0x06, 0, 0, 0x80000000),  # SECCOMP_RET_KILL_PROCESS
        _SockFilter(0x20, 0, 0, 0),  # seccomp_data.nr
    ]
    for syscall_number in syscall_numbers:
        instructions.extend(
            (
                _SockFilter(0x15, 0, 1, syscall_number),
                _SockFilter(0x06, 0, 0, 0x00050000 | errno.EPERM),
            )
        )
    instructions.append(_SockFilter(0x06, 0, 0, 0x7FFF0000))
    filters = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(filters), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "worker no-new-privileges failed")
    address = ctypes.cast(ctypes.pointer(program), ctypes.c_void_p).value
    if address is None or prctl(22, 2, address, 0, 0) != 0:  # PR_SET_SECCOMP, FILTER
        raise OSError(ctypes.get_errno(), "worker seccomp installation failed")


def verify_native_network_isolation() -> None:
    """Prove native libc socket and io_uring entry points receive kernel EPERM."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError("native worker network isolation requires Linux")
    _install_network_seccomp()
    libc = ctypes.CDLL(None, use_errno=True)
    native_socket = libc.socket
    native_socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
    native_socket.restype = ctypes.c_int
    descriptor = native_socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    if descriptor >= 0:
        os.close(descriptor)
        raise RuntimeError("worker native network syscall was not blocked")
    if ctypes.get_errno() != errno.EPERM:
        raise RuntimeError("worker native network syscall failed with an unexpected error")
    native_syscall = libc.syscall
    native_syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    if native_syscall(425, 0, 0) != -1 or ctypes.get_errno() != errno.EPERM:
        raise RuntimeError("worker native io_uring syscall was not blocked")


def _apply_memory_limit(memory_limit_mb: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    limit = memory_limit_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def query_worker_main(
    connection: Connection,
    parquet_root: str,
    max_response_bytes: int,
    memory_limit_mb: int,
) -> None:
    """Child process entrypoint; no identity, token, network, or write capability is passed."""

    try:
        _apply_memory_limit(memory_limit_mb)
        _install_network_seccomp()
        _deny_network()
        import stram_data
        from stram_data.data_interface import INTERFACE_VERSION, DataInterface

        from stram_quant_mcp.tool_adapter import QuantToolAdapter

        adapter = QuantToolAdapter(DataInterface(parquet_root))
        connection.send_bytes(
            json.dumps(
                {
                    "ready": True,
                    "policy_version": adapter.policy_version,
                    "stram_data_version": stram_data.__version__,
                    "interface_version": INTERFACE_VERSION,
                }
            ).encode()
        )
        while True:
            raw = connection.recv_bytes(1024 * 1024)
            request = _decode_object(raw)
            if request.get("control") == "shutdown":
                return
            tool = request.get("tool")
            arguments = request.get("arguments")
            request_id = request.get("request_id")
            if (
                not isinstance(tool, str)
                or not isinstance(arguments, dict)
                or not isinstance(request_id, str)
            ):
                _send_error(connection, ToolInputError.code)
                continue
            try:
                result = adapter.execute(tool, arguments, request_id)
                payload = result.model_dump(mode="json")
                response = json.dumps(
                    {"ok": True, "payload": payload},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                if len(response) > max_response_bytes:
                    _send_error(connection, ToolResourceLimitError.code)
                else:
                    connection.send_bytes(response)
            except QuantToolError as error:
                _send_error(connection, error.code)
            except Exception:
                _send_error(connection, ToolUnavailableError.code)
    except Exception:
        with contextlib.suppress(BrokenPipeError, OSError):
            connection.send_bytes(b'{"ready":false}')
    finally:
        connection.close()


def _send_error(connection: Connection, code: str) -> None:
    connection.send_bytes(json.dumps({"ok": False, "error": code}).encode())
