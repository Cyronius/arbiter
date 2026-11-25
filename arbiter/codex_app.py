"""A small client for the Codex app-server daemon, speaking its own protocol.

Codex 0.17x runs a shared app server (``codex app-server daemon``). Its control
socket is ``$CODEX_HOME/app-server-control/app-server-control.sock`` and carries
JSON-RPC 2.0 over WebSocket (one JSON message per text frame). A client sends
``initialize`` with ``clientInfo``, then the ``initialized`` notification, and may
then call methods such as ``thread/loaded/list``, ``thread/resume``,
``turn/start`` and ``turn/steer``. The method shapes come from
``codex app-server generate-json-schema``.

Only the parts Arbiter needs are implemented, using the standard library alone.
"""

import base64
import itertools
import json
import os
import socket
import struct
import time
from pathlib import Path

CLIENT = {"name": "arbiter", "title": "Arbiter board", "version": "1"}


class CodexUnavailable(Exception):
    pass


class RpcError(CodexUnavailable):
    def __init__(self, method, error):
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"{method}: {error.get('message') if isinstance(error, dict) else error}")


def control_socket() -> str:
    return str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "app-server-control" / "app-server-control.sock")


class Connection:
    def __init__(self, path: str | None = None, timeout: float = 10.0):
        self.timeout = timeout
        self.ids = itertools.count(1)
        self.notifications = []
        self.buffer = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path or control_socket())
            self._handshake()
            self.request("initialize", {"clientInfo": CLIENT})
            self.notify("initialized")
        except (OSError, ValueError) as exc:
            self.close()
            raise CodexUnavailable(f"Codex app server unreachable: {exc}") from exc

    # -- WebSocket framing (RFC 6455, client side) --------------------------------

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ValueError("connection closed during handshake")
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise ValueError("oversized handshake")
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if not head.startswith(b"HTTP/1.1 101"):
            raise ValueError(f"upgrade refused: {head.splitlines()[0][:100]!r}")

    def _send_frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ValueError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _recv_message(self) -> dict:
        parts = []
        while True:
            b1, b2 = self._read(2)
            opcode, length = b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length > 64 * 1024 * 1024:
                raise ValueError("oversized frame")
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ValueError("server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if b1 & 0x80:
                return json.loads(b"".join(parts))

    # -- JSON-RPC ----------------------------------------------------------------

    def notify(self, method: str, params: dict | None = None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send_frame(0x1, json.dumps(message).encode())

    def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        rid = next(self.ids)
        self._send_frame(0x1, json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode())
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            message = self._recv_message()
            if message.get("id") == rid and "method" not in message:
                if "error" in message:
                    raise RpcError(method, message["error"])
                return message.get("result")
            if "method" in message and "id" in message:
                # A server request (an approval prompt, say) is for the thread's own client, not Arbiter.
                self._send_frame(0x1, json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                                  "error": {"code": -32601, "message": "Arbiter does not answer server requests"}}).encode())
            elif "method" in message:
                self.notifications.append(message)

    def wait_for(self, predicate, timeout: float = 60.0) -> dict | None:
        """Read notifications until one satisfies `predicate`, or return None at the deadline."""
        for message in self.notifications:
            if predicate(message):
                return message
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                message = self._recv_message()
            except socket.timeout:
                return None
            if "method" in message and "id" not in message:
                self.notifications.append(message)
                if predicate(message):
                    return message
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def text_input(text: str) -> list:
    return [{"type": "text", "text": text, "text_elements": []}]


def loaded_threads(conn: Connection) -> set[str]:
    threads, cursor = set(), None
    for _ in range(50):
        page = conn.request("thread/loaded/list", {"cursor": cursor, "limit": 200})
        threads.update(page.get("data") or [])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return threads


def deliver(thread_id: str, text: str) -> str:
    """Send `text` to an open Codex conversation as the user's next input.

    An idle conversation starts a new turn; a busy one has the text steered into its
    running turn. Only conversations the shared service already has open qualify, so
    Arbiter never loads a thread that another process might also be writing.
    Returns "turn" or "steer"."""
    with Connection() as conn:
        if thread_id not in loaded_threads(conn):
            raise CodexUnavailable("the conversation is not open in the shared Codex service")
        status = (conn.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"].get("status") or {}).get("type")
        if status == "active":
            try:
                turns = conn.request("thread/turns/list", {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"}).get("data")
            except RpcError:  # ephemeral threads have no turn history API
                turns = conn.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"].get("turns")
            running = next((t for t in reversed(turns or []) if t.get("status") == "inProgress"), None)
            if running:
                conn.request("turn/steer", {"threadId": thread_id, "expectedTurnId": running["id"], "input": text_input(text)})
                return "steer"
        conn.request("turn/start", {"threadId": thread_id, "input": text_input(text)})
        return "turn"
