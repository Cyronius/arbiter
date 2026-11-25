"""Interactive terminal entry points for the local operator."""
import os
import select
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def open_window(command, sid):
    launcher = shutil.which("xdg-terminal-exec")
    if not launcher or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ValueError(f"Desktop terminal unavailable. Run arbiter {command} {sid} in a terminal.")
    proc = subprocess.Popen([launcher, "--", sys.executable, "-I", str(Path(__file__).with_name("entry.py")),
                             command, sid], start_new_session=True)
    try:
        if proc.wait(timeout=.15):
            raise ValueError(f"Terminal launcher failed. Run arbiter {command} {sid} in a terminal.")
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, daemon=True).start()


def show(value):
    print("".join(c for c in str(value) if c.isprintable() or c in "\n\t"), flush=True)


def run(client, command, sid):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Run this command in an interactive terminal.")
    if command == "resume-terminal":
        child = client.action("terminal_claim", session=sid, pid=os.getpid())
        provider = child["kind"]
        try:
            os.chdir(child["cwd"])
            argv = [provider, "resume" if provider == "codex" else "--resume", child["external_id"]]
            env = os.environ.copy()
            for key in ("ARBITER_SESSION", "ARBITER_JOB", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID"):
                env.pop(key, None)
            os.execvpe(provider, argv, env)
        except OSError:
            client.action("finish", session=child["id"], failed=True, safe=True, status="Terminal launch failed")
            raise
    state = client.snapshot()
    session = state["sessions"].get(sid, {})
    if session.get("terminal_action") != "attach-terminal":
        raise ValueError("This session has no live Arbiter terminal connection.")
    cursor = state["cursor"]
    show(f"Attached to {session['name']}. Type a message and press Enter. Ctrl-D detaches.")
    show(session["status"])
    while True:
        events = client.request(f"/api/events?after={cursor}")
        for event in events:
            cursor = event["id"]
            if event["session"] != sid:
                continue
            data = event["data"]
            if "text" in data:
                show(data["text"])
            if event["kind"] == "approval.requested":
                show(f"Approval {data['approval']} requested. Answer on the board or with arbiter answer.")
            if event["kind"] in {"session.finished", "session.failed", "session.reaped", "session.expired"}:
                return
        if select.select([sys.stdin], [], [], 0 if events else .5)[0]:
            line = sys.stdin.readline()
            if not line:
                return
            if line.strip():
                try:
                    client.action("terminal_input", session=sid, text=line.rstrip("\n"))
                except RuntimeError as exc:
                    show(exc)
