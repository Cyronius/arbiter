"""Recovery eligibility, private launch records, and supervised dispatch."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import transcripts
from .launch import isolated_options

DELAYS = (30, 120)
OPTIONS = ("preemptible", "yield_signal", "requeue", "measure")


def validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("invalid launch record")
    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv) or not argv[0]:
        raise ValueError("invalid launch argument vector")
    bundle = record.get("bundle")
    if not isinstance(bundle, dict) or bundle.keys() - {"cpu", "memory_mb", "exclusive", "label", "priority", "measure", "eta"}:
        raise ValueError("invalid launch resource bundle")
    return {"argv": argv, "bundle": {"exclusive": [], "cpu": 0, "memory_mb": 0, **bundle}, **{key: record.get(key) is True for key in OPTIONS}}


def unavailable(store, s, *, inspect_files=True, terminal=False):
    if s["state"] not in {"finished", "failed"}:
        return s.get("stale_reason") or "The attempt is still active."
    if s["kind"] == "job":
        if not s.get("recorded"):
            return "No launch record. The display label cannot replay a command."
    elif s["kind"] in {"claude", "codex", "gaia"}:
        if not terminal and not {"resume-thread", "resume-session"}.intersection(s["capabilities"]):
            return "This conversation has no managed adapter registration."
        guid = s.get("external_id")
        if not transcripts.valid_id(guid, s["kind"]):
            return "The provider conversation GUID is missing or invalid."
        if any(other["state"] not in {"finished", "failed"} and other.get("external_id") == guid
               and other["kind"] == s["kind"] for other in store.state["sessions"].values()):
            return "This provider conversation already has an active session."
        if inspect_files and not transcripts.can_resume(s["kind"], guid):
            return "The local provider transcript is missing or unreadable."
    else:
        return "This session has no managed recovery adapter."
    if inspect_files and store.workers_alive(s):
        return "Tracked work is still running."
    if not os.path.isabs(s["cwd"]) or not os.path.isdir(s["cwd"]):
        return "The original working directory is missing or unavailable."
    return None


def dispatch(store, url, children):
    """The child reads a complete ticket through stdin only after commit. EOF abandons it."""
    children[:] = [p for p in children if p.poll() is None]
    with store.lock:
        pending = [s["id"] for s in store.state["sessions"].values()
                   if s.get("dispatch_pending") and not s.get("pid") and s["state"] not in {"finished", "failed"}]
        due = [s["id"] for s in store.state["sessions"].values() if s.get("retry_at", float("inf")) <= time.time()]
    for sid in due:
        try:
            store.apply("auto_recover", {"session": sid})
        except ValueError as exc:
            store.apply("retry_blocked", {"session": sid, "reason": str(exc)})
    for sid in pending:
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-I", str(Path(__file__).with_name("recovery_worker.py").resolve())],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **isolated_options(background=True))
        children.append(proc)
        with proc.stdin:
            ticket = store.apply("dispatch", {"session": sid, "pid": proc.pid})
            if ticket:
                try:
                    proc.stdin.write(json.dumps(dict(ticket, url=url)).encode())
                    proc.stdin.flush()
                except BrokenPipeError:
                    pass  # Owner reconciliation re-arms a ticket with no tracked workload.
