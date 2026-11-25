"""Deliver board messages straight into a running Claude Code session.

Claude Code sessions accept cross-session messages on a private Unix socket. This
module speaks that wire format, copied from Claude Code 2.1.x (peerProtocol 1):

* The session registers itself in ``~/.claude/sessions/<pid>.json`` with its
  ``messagingSocketPath``, ``sessionId`` and ``peerProtocol``.
* Its auth token is in ``~/.claude/sessions/<pid>.<sha256(socket path)>.key``
  as ``{"peerToken": "<32 hex>", ...}`` (mode 0600, same user only).
* A sender writes newline-delimited JSON: first ``{"type": "auth", "token": ...}``,
  then ``{"msgV": 1, "msg_id": <uuid>, "type": "user", "priority": "next",
  "message": {"role": "user", "content": "<cross-session-message ...>"}}``.

The format is undocumented and may change with a Claude Code update. Every check
below fails closed (returns None / raises PeerUnavailable) so the caller can fall
back to Arbiter's hook-delivered inbox.
"""

import hashlib
import json
import os
import re
import socket
import stat
import uuid
from pathlib import Path

PROTOCOL = 1
TAG = "cross-session-message"
SENDER_NAME = "Arbiter board (operator)"
MAX_CHARS = 60000
TOKEN = re.compile(r"[0-9a-f]{32}")


class PeerUnavailable(Exception):
    pass


def sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"


def _read_private(path: Path, limit: int = 65536) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PeerUnavailable(f"{path.name} is not a regular file owned by this user")
        return file.read(limit + 1)[:limit].decode()


def inbox(pid: int, conversation_id: str) -> tuple[str, str]:
    """Return (socket path, token) for the live session `pid` running `conversation_id`."""
    if not isinstance(pid, int) or pid <= 0:
        raise PeerUnavailable("no process")
    folder = sessions_dir()
    try:
        record = json.loads(_read_private(folder / f"{pid}.json"))
    except (OSError, ValueError) as exc:
        raise PeerUnavailable(f"no registry entry for pid {pid}: {exc}") from exc
    if record.get("peerProtocol") != PROTOCOL:
        raise PeerUnavailable(f"unsupported peer protocol {record.get('peerProtocol')!r}")
    if record.get("sessionId") != conversation_id:
        raise PeerUnavailable("the process now runs a different conversation")
    path = record.get("messagingSocketPath")
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith(".sock"):
        raise PeerUnavailable("the session has no messaging socket")
    key = folder / f"{pid}.{hashlib.sha256(path.encode()).hexdigest()}.key"
    try:
        token = json.loads(_read_private(key, 4096)).get("peerToken")
    except (OSError, ValueError) as exc:
        raise PeerUnavailable(f"no messaging key: {exc}") from exc
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise PeerUnavailable("malformed messaging key")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PeerUnavailable(f"socket missing: {exc}") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PeerUnavailable("socket is not this user's")
    return path, token


def envelope(text: str, sender: str = SENDER_NAME) -> str:
    """Wrap text the way Claude Code frames a cross-session message, escaping a closing tag in the body."""
    name = re.sub(r'["<>\n\r]', "", sender).strip()
    body = re.sub(rf"<(?=/?{TAG})", r"<\\", text, flags=re.IGNORECASE)
    return f'<{TAG} from-name="{name}">\n{body}\n</{TAG}>'


def send(pid: int, conversation_id: str, text: str, sender: str = SENDER_NAME, timeout: float = 2.0) -> str:
    """Deliver `text` to the session as a queued prompt. Returns the message id."""
    if not text or len(text) > MAX_CHARS:
        raise PeerUnavailable("message is empty or too long for cross-session delivery")
    path, token = inbox(pid, conversation_id)
    msg_id = str(uuid.uuid4())
    lines = [{"type": "auth", "token": token},
             {"msgV": 1, "msg_id": msg_id, "type": "user", "priority": "next",
              "message": {"role": "user", "content": envelope(text, sender)}}]
    payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(path)
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            # The session closes the connection once it has read the lines; a reset here means it refused them.
            while conn.recv(4096):
                pass
    except OSError as exc:
        raise PeerUnavailable(f"socket delivery failed: {exc}") from exc
    return msg_id
