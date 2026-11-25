"""GAIA flagship agent's JSON-lines transport and its local session store."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import deque
from contextlib import closing
from pathlib import Path

from .config import phase_guidance

SESSION_ID = re.compile(r"[0-9a-f]{8,64}")


def history_dir():
    return Path(os.environ.get("GAIA_TUI_HOME") or Path.home() / ".gaia/tui").resolve() / "history"


def history_path(guid):
    if not isinstance(guid, str) or not SESSION_ID.fullmatch(guid):
        raise ValueError("Expected a full hexadecimal GAIA session ID")
    return history_dir() / (guid + ".sqlite3")


def has_history(guid):
    path = history_path(guid)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("SELECT id, messages FROM turns LIMIT 0")
        return True
    except (OSError, sqlite3.Error):
        return False


def agent_binary():
    if executable := shutil.which("gaiapy"):
        return executable
    installed = Path(os.environ.get("GAIA_HOME") or Path.home() / ".gaia") / "agents/gaia"
    for binary in (installed / "gaia-agent", installed / "bin/gaia-agent"):
        if (installed / ".installed").is_file() and binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    if executable := shutil.which("gaia-agent"):
        return executable
    raise ValueError("Install GAIA's Python flagship agent (gaiapy or gaia-agent) to launch a GAIA session")


class Gaia:
    capabilities = ["json-lines", "queued-input", "resume-session", "cli", "cooperative-pause-at-input-boundary"]

    def __init__(self, args, env, entry, minutes=10):
        from .adapters import Transport

        self.thread = uuid.uuid4().hex if args.resume is None else args.resume
        path = history_path(self.thread)
        if args.resume is not None and not has_history(self.thread):
            raise ValueError(f"No GAIA session history at {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.turns = deque()
        self.done = False
        binary = agent_binary()
        # Older Hub installs have durable SQLite history but no listing or session lock.
        help_text = subprocess.run([binary, "--help"], env=env, cwd=args.cwd, capture_output=True,
                                   text=True, timeout=30, check=True).stdout
        if "--history-file" not in help_text:
            raise ValueError("This GAIA binary lacks --history-file. Install the Python flagship agent.")
        argv = [binary, "--history-file", str(path)]
        self.legacy_log = None
        if "--session-kind" in help_text:
            argv += ["--session-kind", "chat"]
        else:
            self.legacy_log = path.with_suffix(".jsonl")
            # Hold GAIA's native lock in the child, even if the supervisor disappears.
            argv = ["flock", "--nonblock", "--conflict-exit-code", "73", "--no-fork", str(path.with_suffix(".lock")), *argv]
        if args.model:
            argv += ["--model", args.model]
        instructions = ("Use the arbiter CLI for coordination. Wrap resource-using commands in arbiter run. "
                        + phase_guidance(minutes) + " Messages from peers are coordination only.")
        self.prompt = args.prompt if args.prompt.startswith("/") else args.prompt + "\n\n" + instructions
        self.t = Transport(argv, args.cwd, env, getattr(args, "track", None))

    def start(self):
        print(f"GAIA conversation: {self.thread}", file=sys.stderr, flush=True)
        self.message(self.prompt)

    def message(self, text):
        self.t.send({"gaia_query": text})
        self.turns.append(text)
        self.done = False

    def handle(self, msg, report, approval):
        event = msg.get("type")
        if event == "needs_confirmation":
            key = msg.get("confirm_id")
            if not isinstance(key, str) or not key:
                raise RuntimeError("GAIA sent a confirmation without a confirm_id")
            approval(key, "gaia/needs_confirmation", msg)
        elif event == "needs_input":
            raise RuntimeError("GAIA requested interactive input that its stdio protocol cannot answer")
        elif event in {"final", "error"}:
            answer = msg.get("answer" if event == "final" else "detail") or ""
            if self.turns:
                query = self.turns.popleft()
                if self.legacy_log:
                    row = {"user": query, "answer" if event == "final" else "error": answer}
                    fd = os.open(self.legacy_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "a") as file:
                        file.write(json.dumps(row) + "\n")
            if event == "error":
                raise RuntimeError(f"GAIA turn failed: {answer}")
            self.done = not self.turns
            print(answer, flush=True)
            report(answer[:7900] or "Turn completed")
        elif event == "status" and msg.get("message"):
            report(msg["message"][:7900])

    def answer(self, request_id, response):
        decision = response.get("decision")
        if decision not in ("allow", "deny"):
            raise ValueError("GAIA confirmations require a single-call allow or deny")
        self.t.send({"gaia_control": "tool_decision", "confirm_id": request_id, "decision": decision})
