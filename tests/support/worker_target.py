from __future__ import annotations

import json
import os
import socket
import sys
import time
from multiprocessing.connection import Connection

from stram_quant_mcp.worker import _deny_network, verify_native_network_isolation


def controlled_worker(
    connection: Connection,
    _parquet_root: str,
    _max_response_bytes: int,
    _memory_limit_mb: int,
) -> None:
    connection.send_bytes(b'{"ready":true}')
    while True:
        request = json.loads(connection.recv_bytes())
        if request.get("control") == "shutdown":
            return
        arguments = request.get("arguments", {})
        if arguments.get("crash"):
            os._exit(17)
        delay = float(arguments.get("delay", 0))
        if delay:
            time.sleep(delay)
        connection.send_bytes(
            json.dumps(
                {
                    "ok": True,
                    "payload": {
                        "request_id": request["request_id"],
                        "worker_pid": os.getpid(),
                    },
                }
            ).encode()
        )


def network_probe_worker(
    connection: Connection,
    _parquet_root: str,
    _max_response_bytes: int,
    _memory_limit_mb: int,
) -> None:
    native_blocked: bool | None = None
    if sys.platform.startswith("linux"):
        verify_native_network_isolation()
        native_blocked = True
    _deny_network()
    connection.send_bytes(b'{"ready":true}')
    while True:
        request = json.loads(connection.recv_bytes())
        if request.get("control") == "shutdown":
            return
        blocked = False
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        except OSError:
            blocked = True
        connection.send_bytes(
            json.dumps(
                {
                    "ok": True,
                    "payload": {"blocked": blocked, "native_blocked": native_blocked},
                }
            ).encode()
        )
