"""Transactional state machine. No network or agent-specific policy lives here."""
from __future__ import annotations

import copy
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import guard, operator, recovery, transcripts
from .config import Config, phase_guidance, validate_operator, validate_phase_minutes
from .system import Probe

TERMINAL = {"finished", "failed"}
SCHEMA_VERSION = 3
PRUNE_INTERVAL = 600
DISK_INTERVAL = 15
QUEUE_PROTECTION_S = 30
SHORT_PHASE_S = 600
STALE_CHECK_TIMEOUT_S = 60
STALE_CHECK_COOLDOWN_S = 300
KINDS = {"external", "codex", "claude", "job", "hold", "demo"}
PROVIDERS = {"codex", "claude"}
OPERATOR_FIELDS = ("provider", "model", "manage_resources", "approve_inbox", "reply_messages", "review_status", "instructions")
SCHEDULER_SWITCHES = ("auto_approve_inbox", "preserve_interactivity")
ACTIVE = {"requested", "granted", "suspended"}


def ident(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


def number(value: Any, name: str, low: float = 0, high: float = 1e9) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def text(value: Any, name: str, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text, at most {limit} characters")
    return value


def optional_text(value: Any, name: str, limit: int = 8000) -> str | None:
    return None if value is None or value == "" else text(value, name, limit)


def clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def grace(seconds: float) -> float:
    """How long past its ETA an unattended hold survives before it is treated as abandoned."""
    return min(3600, max(600, seconds * .5))


class Store:
    def __init__(self, path, cpu: float = 4, memory_mb: float = 4096, stale_seconds: float = 45,
                 headroom_mb: float = 512, retention_days: float | None = None, probe=None, config: Config | None = None,
                 initial_telemetry: dict | None = None):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if str(path) != ":memory:":
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # A workload can start immediately after a tracking acknowledgement.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, session TEXT, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS launches (root TEXT PRIMARY KEY, record TEXT NOT NULL)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {}
        try:
            self.migrate()
        except Exception:
            self.db.close()
            raise
        self.capacity = {"cpu": number(cpu, "cpu", .01), "memory_mb": number(memory_mb, "memory_mb", 1)}
        self.stale_seconds = stale_seconds
        self.headroom_mb = headroom_mb
        self.retention_days = retention_days
        self.probe = probe or Probe()
        self.config = config or Config(data={})
        telemetry = initial_telemetry or {}
        self.available_mb = telemetry.get("available_mb")
        self.cpu_busy = telemetry.get("cpu_busy")
        self.gpu_pressure = telemetry.get("gpu_pressure", [])
        self.recovered_since: float | None = None
        self.pending: list = []
        self.prune_events_before: float | None = None
        self.last_prune = time.monotonic()
        self.last_disk = 0.0
        self.disk_sizes: dict[str, float] = {}
        # Staleness uses a monotonic clock that is never persisted: a wall-clock jump must not
        # expire every session at once, and a restart marks all sessions stale regardless.
        self.beats: dict[str, float] = {}
        # A restarted daemon cannot infer that old owners stopped using resources, unless it
        # can see their processes: those are checked directly.
        self.apply("reconcile", {})

    def migrate(self) -> None:
        """Bring a loaded state blob up to SCHEMA_VERSION. Add a step per schema change."""
        state = self.state
        for key in ("sessions", "reservations", "commands", "messages", "approvals"):
            state.setdefault(key, {})
        state.setdefault("operator_decisions", [])
        state.setdefault("operator_events", {})
        version = state.get("schema_version", 0)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"State was written by a newer Arbiter (schema {version}); refusing to downgrade it")
        # 0 -> 1: introduced schema_version; reservations in the removed "cancelled" state are released.
        for r in state["reservations"].values():
            if r["state"] == "cancelled":
                r["state"] = "released"
            # Older requests stored the estimate as a deadline from submission.
            if "duration_s" not in r and r.get("eta", 0) > r["created"]:
                r["duration_s"] = r["eta"] - r["created"]
        # 1 -> 2: optional owner PIDs, parents, ETAs, measure and disk fields; all read with defaults.
        if version < 3:
            # Early managed supervisors reported the provider ID before there was a dedicated field.
            booted = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
            for s in state["sessions"].values():
                if s.get("pid") and "boot_id" not in s:
                    # The old schema has only a start tick. A session created before this boot has no surviving PID.
                    s["boot_id"] = "previous boot" if s["created"] < booted else self.boot_id
                if s["kind"] not in PROVIDERS or s.get("external_id"):
                    continue
                for (body,) in self.db.execute("SELECT body FROM events WHERE session=? AND kind='session.status' ORDER BY id", (s["id"],)):
                    line = json.loads(body).get("text", "")
                    match = re.fullmatch(s["kind"].capitalize() + r" conversation (\S+)", line)
                    if match and transcripts.valid_id(match[1]):
                        s.update(provider=s["kind"], external_id=match[1])
                        break
            for s in state["sessions"].values():
                if s["kind"] in PROVIDERS and transcripts.valid_id(s.get("external_id")) and not os.path.isabs(s["cwd"]):
                    cwd = transcripts.initial_cwd(s["kind"], s["external_id"])
                    if cwd:
                        s["cwd"] = cwd
        state["schema_version"] = SCHEMA_VERSION

    def emit(self, event_type: str, actor: str | None = None, **body) -> None:
        self.pending.append((time.time(), event_type, actor, json.dumps(body)))

    def session(self, sid: str) -> dict:
        if sid not in self.state["sessions"]:
            raise ValueError("unknown session")
        return self.state["sessions"][sid]

    def live(self, sid: str) -> dict:
        s = self.session(sid)
        if s["state"] in TERMINAL:
            raise ValueError("session has finished")
        return s

    def family(self, s: dict) -> str:
        return s.get("parent") or s["id"]

    def who(self, sid: str) -> str:
        s = self.state["sessions"].get(sid)
        return s["name"] if s else sid

    def session_for_token(self, token: str) -> str | None:
        """Map a per-session bearer token to its live session (constant-time comparison)."""
        candidate = token.encode()
        with self.lock:
            found = None
            for sid, s in self.state["sessions"].items():
                if hmac.compare_digest(s.get("token", "").encode(), candidate) and s["state"] not in TERMINAL:
                    found = sid
            return found

    def apply(self, action: str, data: dict) -> Any:
        if action == "heartbeat":
            # Heartbeats are the most frequent write and carry no durable meaning: skip the
            # copy/serialize/commit cycle unless the heartbeat revives a stale session.
            with self.lock:
                s = self.live(data.get("session"))
                if s["state"] != "stale":
                    s["heartbeat"] = time.time()
                    self.beats[s["id"]] = time.monotonic()
                    return {"ok": True}
        with self.changed:
            before = copy.deepcopy(self.state)
            saved = (self.available_mb, self.recovered_since, self.cpu_busy, self.gpu_pressure)
            self.pending = []
            self.prune_events_before = None
            try:
                result = self.mutate(action, data)
                self.schedule()
                self.collect_operator_events(before)
                if action == "hook" and not self.pending:
                    # A hook that changed nothing durable (the common case) skips the commit.
                    return copy.deepcopy(result)
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.state),))
                    self.db.executemany("INSERT INTO events(at,kind,session,body) VALUES (?,?,?,?)", self.pending)
                    if self.prune_events_before is not None:
                        # Keep the newest event so the cursor never moves backwards.
                        self.db.execute("DELETE FROM events WHERE at<? AND id<(SELECT MAX(id) FROM events)", (self.prune_events_before,))
                self.changed.notify_all()
                return copy.deepcopy(result)
            except Exception:
                self.db.rollback()
                self.state = before
                self.available_mb, self.recovered_since, self.cpu_busy, self.gpu_pressure = saved
                raise
            finally:
                self.pending = []

    # -- actions -----------------------------------------------------------------
    # Handlers named _g_* take (data, now) and need no session. Handlers named _a_*
    # take (session, data, now). Only these prefixes are reachable through `mutate`.

    def mutate(self, action: str, d: dict) -> Any:
        now = time.time()
        handler = getattr(self, "_g_" + str(action), None)
        if handler:
            return handler(d, now)
        s = self.live(d.get("session"))
        handler = getattr(self, "_a_" + str(action), None)
        if not handler:
            raise ValueError("unknown action")
        return handler(s, d, now)

    def _g_reconcile(self, d: dict, now: float) -> dict:
        mono = time.monotonic()
        for s in list(self.state["sessions"].values()):
            if s["state"] in TERMINAL:
                continue
            if s.get("pid"):
                self.check_owner(s, now, mono)
            elif not s.get("dispatch_pending") and s["kind"] != "hold" and s["state"] != "stale":
                self.mark_stale(s, "daemon restarted; reservations retained")
        return {"ok": True}

    def mark_stale(self, s: dict, reason: str) -> None:
        if s["state"] == "stale":
            if s.get("stale_reason") == reason:
                return
        else:
            s["previous_state"] = s["state"]
        s["state"] = "stale"
        s["stale_reason"] = reason
        self.emit("session.stale", s["id"], reason=reason)

    def revive(self, s: dict, why: str) -> None:
        if s["state"] != "stale":
            return
        s["state"] = s.pop("previous_state", "running")
        s.pop("stale_reason", None)
        self.emit("session.reconnected", s["id"], state=s["state"], reason=why)

    def check_owner(self, s: dict, now: float, mono: float) -> None:
        """A session with an owner PID lives exactly as long as that process (or the work it started)."""
        same_boot = s.get("boot_id", self.boot_id) == self.boot_id
        if same_boot and self.probe.alive(s["pid"], s.get("pid_start")):
            self.beats[s["id"]] = mono
            s.pop("worker_survived", None)
            self.revive(s, "owner process is running")
            return
        if self.workers_alive(s):
            s["worker_survived"] = True
            self.mark_stale(s, "owner process exited but its commands still run; reservations retained until they exit")
            return
        if s.get("dispatch_pending") and not s.get("cancel_requested"):
            # The bootstrap cannot execute the program until track commits. Reuse this claim.
            for key in ("pid", "pid_start", "boot_id"):
                s.pop(key, None)
            return
        s["outcome"] = "cancelled" if s.get("cancel_requested") else "unknown"
        self.end(s, now, failed=True, status="Owner process exited; " + ("cancelled" if s.get("cancel_requested") else "outcome unknown"), event="session.reaped")

    def workers_alive(self, s):
        if s.get("boot_id", self.boot_id) != self.boot_id:
            return False
        for group in s.get("groups", []):
            original = s.get("group_starts", {}).get(str(group))
            current = self.probe.start_time(group)
            if original is not None and current is not None and original != current:
                continue
            if self.probe.group_alive(group):
                return True
        return False

    def _g_register(self, d: dict, now: float) -> dict:
        sid = ident("s")
        kind = d.get("kind", "external")
        if kind not in KINDS:
            raise ValueError("unknown session kind")
        cwd = d.get("cwd", "")
        if not isinstance(cwd, str) or len(cwd) > 1000:
            raise ValueError("cwd must be text, at most 1000 characters")
        capabilities = d.get("capabilities", [])
        if not isinstance(capabilities, list) or len(capabilities) > 32:
            raise ValueError("capabilities must be a list with at most 32 entries")
        s = {"id": sid, "name": text(d.get("name"), "name", 100), "kind": kind,
             "state": "running", "created": now, "heartbeat": now, "status": optional_text(d.get("status"), "status") or "Registered",
             "cwd": cwd, "preemptible": bool(d.get("preemptible", False)), "yields": bool(d.get("yields", False)),
             "capabilities": [text(c, "capability", 100) for c in capabilities], "token": secrets.token_urlsafe(32)}
        if d.get("parent"):
            parent = self.live(d["parent"])
            s["parent"] = self.family(parent)
        if d.get("pid") is not None:
            pid = int(number(d["pid"], "pid", 1, 2**31))
            started = d.get("pid_start")
            started = int(number(started, "pid_start", 0, 2**63)) if started is not None else self.probe.start_time(pid)
            if not self.probe.alive(pid, started):
                raise ValueError("owner process is not running")
            s["pid"], s["pid_start"], s["boot_id"] = pid, started, self.boot_id
        if d.get("provider") is not None:
            if d["provider"] not in PROVIDERS:
                raise ValueError("unknown provider")
            s["provider"], s["external_id"] = d["provider"], text(d.get("external_id"), "external_id", 200)
        if "launch" in d:
            if kind != "job" or not os.path.isabs(cwd):
                raise ValueError("a recorded job needs an absolute working directory")
            record = recovery.validate_record(d["launch"])
            self.db.execute("INSERT INTO launches VALUES (?, ?)", (sid, json.dumps(record)))
            s.update(recorded=True, retry_limit=2 if d.get("auto_retry") is True else 0, retries_used=0)
        self.state["sessions"][sid] = s
        self.beats[sid] = time.monotonic()
        self.emit("session.started", sid, name=s["name"], kind=kind, parent=s.get("parent"))
        return s

    def _g_attach(self, d: dict, now: float) -> dict:
        """Find or register the Arbiter session of an existing Claude Code or Codex conversation."""
        provider, external = d.get("provider"), text(d.get("external_id"), "external_id", 200)
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        pid = d.get("pid")
        sessions = self.state["sessions"].values()
        found = next((s for s in sessions if s["state"] not in TERMINAL and s.get("provider") == provider
                      and s.get("external_id") == external), None)
        if found:
            if pid and pid != found.get("pid") and "hook" in found["capabilities"]:
                started = self.probe.start_time(pid)
                if self.probe.alive(pid, started):
                    found["pid"], found["pid_start"], found["boot_id"] = pid, started, self.boot_id
            found["heartbeat"] = now
            self.beats[found["id"]] = time.monotonic()
            self.revive(found, "conversation is active again")
            return found
        cwd = d.get("cwd") or ""
        name = d.get("name") or f"{os.path.basename(cwd.rstrip('/')) or provider}-{external.replace('-', '')[:4]}"
        # One process may host several conversations (Codex app-server threads, /clear in Claude Code),
        # so a new conversation never replaces another; each ends on its own SessionEnd or process exit.
        return self._g_register({"name": name[:100], "kind": provider, "cwd": cwd, "pid": pid, "provider": provider,
                                 "external_id": external, "capabilities": ["hook", "cli"], "status": "Attached"}, now)

    def _g_hook(self, d: dict, now: float) -> dict:
        """One round trip for a lifecycle hook: identify the session, guard a command, deliver the inbox."""
        if d.get("session"):
            s = self.live(d["session"])
            s["heartbeat"] = now
            self.beats[s["id"]] = time.monotonic()
            if not s.get("external_id") and d.get("provider") == s["kind"] and s["kind"] in PROVIDERS:
                if transcripts.valid_id(d.get("external_id")):
                    self._a_conversation(s, d, now)
        else:
            s = self._g_attach(d, now)
        out = {"session": s["id"], "name": s["name"], "preferred_phase_minutes": self.preferred_phase_minutes()}
        if d.get("event") == "end":
            if self.workers_alive(s):
                raise ValueError("The supervisor must finish after tracked work exits")
            self.end(s, now, failed=False, status=optional_text(d.get("status"), "status") or "Session ended")
            out["state"] = s["state"]
            return out
        if d.get("command") is not None:
            out["guard"] = self.guard(s, str(d["command"])[:20000])
        if d.get("collect"):
            out["inbox"] = self.collect(s, now)
        out["state"] = s["state"]
        return out

    def guard(self, s: dict, command: str) -> dict:
        """Decide whether a shell command an agent is about to run must wait for another session."""
        cfg = self.config
        g = cfg["guard"]
        allow = {"decision": "allow", "wait_inline_s": g["wait_inline_s"]}
        if not g["enabled"]:
            return allow
        light = guard.read_only(command, set(g.get("read_only", [])))
        self_gated = guard.wrapped(command)
        if s["state"] == "draining" and not light and not self_gated:
            c = next((c for c in self.state["commands"].values() if c["session"] == s["id"] and c["action"] == "pause"
                      and c["state"] in {"pending", "delivered", "unanswered"}), None)
            return {"decision": "deny", "resources": [], "reason": (
                "Arbiter asked this session to pause" + (f" ({c['reason']})" if c else "") + ". Do not start new work. "
                "Stop any resource-using commands you started, then run `arbiter checkpoint --safe 'what you finished'`. "
                "Commands are held until the session is resumed.")}
        if s["state"] == "paused" and not light and not self_gated:
            return {"decision": "wait", "resources": ["--resumed"], "wait_inline_s": g["wait_inline_s"],
                    "reason": "This session is paused by Arbiter until it is resumed."}
        if self_gated:
            return allow
        fam = self.family(s)
        held = [r for r in self.state["reservations"].values() if r["state"] == "granted"]
        others = [r for r in held if self.family(self.session(r["session"])) != fam]
        measuring = [r for r in others if r.get("measure")]
        if measuring:
            return {"decision": "wait", "resources": ["--measure"], "wait_inline_s": g["wait_inline_s"],
                    "reason": f"{self.describe(measuring[0])} is measuring; every command waits until it finishes."}
        uses = guard.matched(command, cfg.patterns)
        blocked = []
        for v in uses:
            holders = [r for r in others if v in r["exclusive"]]
            if len(holders) >= cfg.capacity(v):
                blocked.append(f"{v} is held by " + ", ".join(self.describe(r) for r in holders))
        if blocked:
            return {"decision": "wait", "resources": uses, "wait_inline_s": g["wait_inline_s"], "reason": "; ".join(blocked) + "."}
        if uses:
            mine = any(set(uses) & set(r["exclusive"]) for r in held if r not in others)
            if not mine:
                allow = dict(allow, context=(
                    f"Arbiter: this command uses {', '.join(uses)}. Next time wrap it so other sessions wait for you: "
                    f"`arbiter run --exclusive {' --exclusive '.join(uses)} --label '<what>' --eta <minutes>m -- <command>`."))
            return allow
        if not light and g["pressure"] and not any(r not in others for r in held):
            p = self.pressure()
            if not p["ok"]:
                return {"decision": "wait", "resources": ["--pressure"], "wait_inline_s": g["wait_inline_s"],
                        "reason": f"The machine is under pressure ({p['reason']}); new commands wait until it eases."}
        return allow

    def describe(self, r: dict) -> str:
        s = self.session(r["session"])
        out = s["name"]
        if s.get("parent") and s["parent"] in self.state["sessions"]:
            out = f"{self.state['sessions'][s['parent']]['name']}/{out}"
        if r.get("label") and r["label"] != s["name"]:
            out += f" ('{r['label']}')"
        if r.get("eta"):
            out += f" until ~{clock(r['eta'])}"
            if r.get("stale_at") and not s.get("pid"):
                out += f" (stale at {clock(r['stale_at'])})"
        return out

    def pressure(self) -> dict:
        reasons = []
        if self.available_mb is not None and self.available_mb < self.headroom_mb:
            reasons.append(f"{self.available_mb:.0f} MiB free, below the {self.headroom_mb:.0f} MiB headroom")
        limit = self.config["guard"]["cpu_busy_pct"]
        if self.cpu_busy is not None and self.cpu_busy > limit:
            reasons.append(f"CPU {self.cpu_busy:.0f}% busy")
        return {"ok": not reasons, "reason": "; ".join(reasons), "available_mb": self.available_mb, "cpu_busy": self.cpu_busy}

    def collect(self, s: dict, now: float) -> dict:
        """Hand pending requests and unread messages to a hooked agent, recording delivery."""
        commands = []
        for c in self.state["commands"].values():
            if c["session"] == s["id"] and c["state"] == "pending":
                self.deliver_command(c, now)
                commands.append(copy.deepcopy(c))
        messages = []
        for m in self.state["messages"].values():
            if m["from"] != s["id"] and m["to"] in {None, s["id"]} and s["id"] not in m["acks"] and m["created"] >= s["created"]:
                m["acks"].append(s["id"])
                self.emit("message.acknowledged", s["id"], message=m["id"], via="hook")
                messages.append(dict(copy.deepcopy(m), sender=self.who(m["from"]) if m["from"] else "Arbiter"))
        return {"commands": commands, "messages": messages}

    def _g_tick(self, d: dict, now: float) -> dict:
        self.available_mb = d.get("available_mb", self.available_mb)
        self.cpu_busy = d.get("cpu_busy", self.cpu_busy)
        self.gpu_pressure = d.get("gpu_pressure", self.gpu_pressure)
        self.config.refresh()
        mono = time.monotonic()
        for s in list(self.state["sessions"].values()):
            if s["state"] in TERMINAL:
                continue
            if s.get("pid"):
                self.check_owner(s, now, mono)
            elif s["kind"] == "hold":
                stale_at = min((r["stale_at"] for r in self.state["reservations"].values()
                                if r["session"] == s["id"] and r["state"] in ACTIVE and r.get("stale_at")), default=None)
                if stale_at and now > stale_at:
                    self.end(s, now, failed=False, status=f"Hold expired: its ETA passed at {clock(stale_at)} without release or extension",
                             event="session.expired")
            elif not s.get("dispatch_pending") and s["state"] != "stale" and mono - self.beats.setdefault(s["id"], mono) > self.stale_seconds:
                self.mark_stale(s, "heartbeat expired; reservations retained")
        for c in self.state["commands"].values():
            # An overdue check that never reached a hook remains available for delivery.
            if c["state"] == "pending" and c.get("source") == "stale_check":
                continue
            if c["state"] in {"pending", "delivered"} and now > c["deadline"]:
                c["state"] = "unanswered"
                self.emit("command.unanswered", c["session"], command=c["id"], action=c["action"])
        if mono - self.last_disk >= DISK_INTERVAL:
            self.last_disk = mono
            for r in self.state["reservations"].values():
                if r["state"] == "granted" and r.get("disk_gb"):
                    self.disk_sizes[r["id"]] = self.probe.size_gb(r["disk_path"])
        self.memory_pressure(now)
        if self.retention_days and mono - self.last_prune >= PRUNE_INTERVAL:
            self.last_prune = mono
            self.prune(now)
        return {"ok": True}

    def memory_pressure(self, now: float) -> None:
        if self.available_mb is None:
            return
        if self.available_mb < self.headroom_mb:
            self.recovered_since = None
            # One draining worker at a time; a pause request is not a resource release.
            if any(s["state"] == "draining" for s in self.state["sessions"].values()):
                return
            candidates = [r for r in self.state["reservations"].values() if r["state"] == "granted" and r["memory_mb"] > 0
                          and self.session(r["session"])["preemptible"] and self.session(r["session"])["state"] == "running"]
            if candidates:
                victim = min(candidates, key=lambda r: (r["priority"], -r["memory_mb"]))
                self.mutate("control", {"session": victim["session"], "action": "pause", "reason": "Automatic memory-pressure request; reach a safe stopping point and release job memory"})
                self.session(victim["session"])["auto_paused"] = True
        elif self.available_mb >= self.headroom_mb * 2:
            self.recovered_since = self.recovered_since or now
            if now - self.recovered_since >= 15:
                for s in list(self.state["sessions"].values()):
                    if s.get("auto_paused") and s["state"] == "paused":
                        self.mutate("control", {"session": s["id"], "action": "resume", "reason": "Memory headroom recovered for 15 seconds"})
        else:
            self.recovered_since = None

    def prune(self, now: float) -> None:
        """Forget sessions that ended more than `retention_days` ago, with everything they own."""
        cutoff = now - self.retention_days * 86400
        old = {sid for sid, s in self.state["sessions"].items()
               if s["state"] in TERMINAL and s.get("ended", s["heartbeat"]) < cutoff}
        # Retain the entire lineage while any attempt is still within retention.
        keep = {s.get("original", s["id"]) for s in self.state["sessions"].values() if s["id"] not in old or s.get("retry_at")}
        old = {sid for sid in old if self.session(sid).get("original", sid) not in keep}
        state = self.state
        counts = {"sessions": len(old)}
        for key, owner in (("reservations", lambda v: v["session"]), ("commands", lambda v: v["session"]),
                           ("approvals", lambda v: v["session"]), ("messages", lambda v: v["from"])):
            doomed = [k for k, v in state[key].items() if owner(v) in old or (key == "messages" and v["to"] in old)]
            for k in doomed:
                del state[key][k]
                self.disk_sizes.pop(k, None)
            counts[key] = len(doomed)
        for sid in old:
            self.db.execute("DELETE FROM launches WHERE root=?", (sid,))
            del state["sessions"][sid]
            self.beats.pop(sid, None)
        if old:
            self.emit("state.pruned", None, **counts)
        self.prune_events_before = cutoff

    def _a_heartbeat(self, s: dict, d: dict, now: float) -> dict:
        s["heartbeat"] = now
        self.beats[s["id"]] = time.monotonic()
        self.revive(s, "heartbeat resumed")
        return {"ok": True}

    def _a_report(self, s: dict, d: dict, now: float) -> dict:
        s["status"] = text(d.get("status"), "status")
        if s["kind"] in PROVIDERS:
            s["last_report"] = s["status"]
        if d.get("checkpoint"):
            s["checkpoint"] = text(d["checkpoint"], "checkpoint")
        details = {k: optional_text(d.get(k), k) for k in ("progress", "blocker", "stopping_point")}
        s["report_details"] = {k: v for k, v in details.items() if v}
        self.emit("session.status", s["id"], text=s["status"], **details)
        cid = d.get("command")
        if cid:
            self.ack(s["id"], cid, "status")
            if s["state"] == "stale" and not s.get("pid") and self.state["commands"][cid].get("source") == "stale_check":
                self._a_heartbeat(s, {}, now)
        return {"ok": True}

    def _a_track(self, s: dict, d: dict, now: float) -> dict:
        """Record a process group this session started, so its reservations outlive a killed owner only while it runs."""
        pgid = int(number(d.get("pgid"), "pgid", 1, 2**31))
        if s.get("cancel_requested") or s["state"] in {"stale", "paused", "waiting"}:
            raise ValueError("session cannot start work")
        reservation = d.get("reservation") or (s.get("recovery_reservation") if s.get("dispatch_pending") else None)
        if reservation:
            grant = self.state["reservations"].get(reservation)
            if not grant or grant["session"] != s["id"] or grant["state"] != "granted":
                raise ValueError("The workload needs its granted reservation before execution")
        s.pop("dispatch_pending", None)
        groups = s.setdefault("groups", [])
        if pgid not in groups:
            if len(groups) >= 16:
                s.get("group_starts", {}).pop(str(groups.pop(0)), None)
            groups.append(pgid)
        s.setdefault("group_starts", {})[str(pgid)] = self.probe.start_time(pgid)
        return {"ok": True}

    def _a_cancelling(self, s, d, now):
        s["cancel_requested"] = True
        self.emit("session.cancelling", s["id"])
        return {"ok": True}

    def _g_cancel_retry(self, d, now):
        s = self.session(d.get("session"))
        if s.pop("retry_at", None) is None:
            if s.get("retry_cancelled"):
                return {"ok": True}
            raise ValueError("There is no pending retry")
        s["retry_cancelled"] = True
        self.emit("recovery.cancelled", s["id"])
        return {"ok": True}

    def _g_recover(self, d, now):
        s = self.session(d.get("session"))
        if d.get("recovery_action") == "rerun-command" and s["kind"] != "job":
            raise ValueError("Rerun command requires a recorded shell job")
        if d.get("recovery_action") == "resume-conversation" and s["kind"] not in PROVIDERS:
            raise ValueError("Resume conversation requires a managed Claude or Codex session")
        if s.get("successor"):
            return {"id": s["successor"]}
        return self.claim_recovery(s, now, automatic=False, confirm_unknown=d.get("confirm_unknown") is True)

    def _g_auto_recover(self, d, now):
        s = self.session(d["session"])
        if s.get("retry_at", math.inf) <= now and s.get("outcome") == "failure" and s["retries_used"] < s["retry_limit"]:
            return self.claim_recovery(s, now, automatic=True)

    def _g_retry_blocked(self, d, now):
        s = self.session(d["session"])
        if s.pop("retry_at", None) is not None:
            s["retry_blocker"] = d["reason"]
            self.emit("recovery.blocked", s["id"], reason=d["reason"])

    def launch_record(self, s):
        row = self.db.execute("SELECT record FROM launches WHERE root=?", (s.get("original", s["id"]),)).fetchone()
        if not row:
            raise ValueError("Private launch record unavailable")
        return json.loads(row[0])

    def claim_recovery(self, s, now, *, automatic, confirm_unknown=False):
        reason = recovery.unavailable(self, s)
        if reason:
            raise ValueError(reason)
        if s.get("outcome", "unknown") == "unknown" and not confirm_unknown:
            raise ValueError("Outcome unknown. Inspect surviving output and confirm the recovery.")
        root = s.get("original", s["id"])
        if s["kind"] == "job":
            bundle = self.launch_record(s)["bundle"]
        else:
            # Restore the last phase that was active at shutdown, if any.
            bundle = s.get("last_bundle", {})
        child = self._g_register({k: s[k] for k in ("name", "kind", "cwd", "preemptible", "yields", "capabilities") if k in s}, now)
        if s.get("parent") in self.state["sessions"] and self.session(s["parent"])["state"] not in TERMINAL:
            child["parent"] = s["parent"]
        child.update(original=root, predecessor=s["id"], dispatch_pending=True, status="Awaiting recovery supervisor")
        if s.get("recorded"):
            child.update(recorded=True, retry_limit=s["retry_limit"], retries_used=s["retries_used"] + int(automatic))
        else:
            child.update(provider=s["kind"], external_id=s["external_id"])
        child["recovery_reservation"] = self._a_reserve(child, bundle, now, admission_only=True)["id"]
        s.pop("retry_at", None)
        s["successor"] = child["id"]
        self.emit("recovery.claimed", s["id"], original=root, attempt=child["id"], automatic=automatic)
        return {"id": child["id"]}

    def _g_dispatch(self, d, now):
        s = self.session(d["session"])
        if not s.get("dispatch_pending") or s.get("pid") or s["state"] in TERMINAL:
            return None
        pid = d["pid"]
        started = self.probe.start_time(pid)
        if started is None or not self.probe.alive(pid, started):
            return None
        s.update(pid=pid, pid_start=started, boot_id=self.boot_id)
        ticket = {"session": s["id"], "kind": s["kind"], "cwd": s["cwd"], "token": s["token"], "reservation": s["recovery_reservation"]}
        if s["kind"] == "job":
            ticket["record"] = self.launch_record(s)
        else:
            previous = self.session(s["predecessor"])
            checkpoint = previous.get("checkpoint") or previous.get("pause_checkpoint") or previous.get("last_report") or "No checkpoint reported."
            survivors = [f"{child['name']} ({child['id']}, {child['state']})" for child in self.state["sessions"].values()
                         if child.get("parent") in {previous["id"], s["original"]} and child["state"] not in TERMINAL]
            ticket.update(guid=s["external_id"],
                          prompt=f"Resume the interrupted work in this directory. Previous attempt {previous['id']}: "
                                 f"{previous['status']}. Last reported progress: {checkpoint[:1500]}. "
                                 f"Active jobs from the conversation: {', '.join(survivors[:8]) or 'none reported'}. "
                                 "Inspect surviving files and completed work before continuing.")
        return ticket

    def _a_conversation(self, s: dict, d: dict, now: float) -> dict:
        provider, external_id = d.get("provider"), d.get("external_id")
        if provider not in PROVIDERS or s["kind"] != provider or not transcripts.valid_id(external_id):
            raise ValueError("invalid provider conversation")
        if s.get("predecessor") and s.get("external_id") != external_id:
            raise ValueError("Recovered provider conversation ID changed")
        s["provider"], s["external_id"] = provider, external_id
        self.emit("session.conversation", s["id"], provider=provider)
        return {"ok": True}

    def _a_adopt(self, s: dict, d: dict, now: float) -> dict:
        """Hand a granted hold to another process (`hold --pid`), or to its ETA alone (no pid)."""
        if s["kind"] != "hold":
            raise ValueError("only holds can change their owner process")
        if d.get("pid") is None:
            s.pop("pid", None)
            s.pop("pid_start", None)
        else:
            pid = int(number(d["pid"], "pid", 1, 2**31))
            started = self.probe.start_time(pid)
            if not self.probe.alive(pid, started):
                raise ValueError("process is not running")
            s["pid"], s["pid_start"], s["boot_id"] = pid, started, self.boot_id
        return {"ok": True}

    def _a_reserve(self, s: dict, d: dict, now: float, *, admission_only=False) -> dict:
        sid = s["id"]
        if s["state"] in {"draining", "paused", "stale"}:
            raise ValueError("cannot request resources while draining, paused, or stale")
        cpu = number(d.get("cpu", 0), "cpu")
        mem = number(d.get("memory_mb", 0), "memory_mb")
        exclusive = d.get("exclusive", [])
        if not isinstance(exclusive, list) or len(exclusive) > 64:
            raise ValueError("exclusive must be a list with at most 64 names")
        exclusive = sorted(set(text(v, "resource", 250) for v in exclusive))
        measure = bool(d.get("measure", False))
        disk_gb = number(d.get("disk_gb", 0), "disk_gb", 0, 1e6)
        empty = not (cpu or mem or exclusive or measure or disk_gb)
        if empty and not admission_only:
            raise ValueError("request at least one resource")
        if cpu > self.capacity["cpu"] or mem > self.capacity["memory_mb"]:
            raise ValueError("request exceeds configured capacity")
        # A recovered conversation holds an empty admission grant until its first
        # real resource request. Swap them in the same scheduler transaction.
        initial = self.state["reservations"].get(s.get("recovery_reservation"))
        if (initial and initial.get("admission_only") and initial["state"] == "granted"
                and s["kind"] in PROVIDERS and not s.get("dispatch_pending")):
            self._a_release(s, {"reservation": initial["id"]}, now)
        # One atomic bundle per session avoids hold-and-wait deadlocks.
        if any(r["session"] == sid and r["state"] in ACTIVE for r in self.state["reservations"].values()):
            raise ValueError("release existing resource bundle before requesting another")
        rid = ident("r")
        r = {"id": rid, "session": sid, "cpu": cpu, "memory_mb": mem, "exclusive": exclusive,
             "priority": number(d.get("priority", 0), "priority", -100, 100), "created": now,
             "state": "requested", "reason": "Waiting for scheduler"}
        if empty:
            r["admission_only"] = True
        if d.get("label"):
            r["label"] = text(d["label"], "label", 200)
        if measure:
            r["measure"] = True
        if disk_gb:
            path = text(d.get("disk_path"), "disk_path", 1000)
            if not path.startswith("/"):
                raise ValueError("disk_path must be absolute")
            r.update(disk_gb=disk_gb, disk_path=path, disk_device=self.probe.disk(path)["device"])
        if d.get("eta") is not None:
            self.set_eta(r, number(d["eta"], "eta", 1, 30 * 86400), now)
        self.state["reservations"][rid] = r
        self.emit("reservation.requested", sid, reservation=rid, cpu=cpu, memory_mb=mem, exclusive=exclusive,
                  label=r.get("label"), measure=measure, disk_gb=disk_gb or None, eta=r.get("eta"))
        return r

    def set_eta(self, r: dict, seconds: float, now: float) -> None:
        r["duration_s"] = seconds
        r["eta"] = now + seconds
        r["stale_at"] = r["eta"] + grace(seconds)

    def _a_extend(self, s: dict, d: dict, now: float) -> dict:
        seconds = number(d.get("eta"), "eta", 1, 30 * 86400)
        active = [r for r in self.state["reservations"].values() if r["session"] == s["id"] and r["state"] in ACTIVE]
        if not active:
            raise ValueError("session holds no reservation to extend")
        for r in active:
            self.set_eta(r, seconds, now)
            self.emit("reservation.extended", s["id"], reservation=r["id"], eta=r["eta"])
        return active[0]

    def _a_release(self, s: dict, d: dict, now: float) -> dict:
        r = self.state["reservations"].get(d.get("reservation"))
        if not r or r["session"] != s["id"]:
            raise ValueError("unknown reservation for session")
        if s.get("dispatch_pending") and r["id"] == s.get("recovery_reservation"):
            s.update(cancel_requested=True, outcome="cancelled")
            self.end(s, now, failed=False, status="Recovery reservation released before launch")
            return {"ok": True}
        if (s["state"] == "stale" or s["kind"] == "job") and self.workers_alive(s):
            raise ValueError("Tracked work is still running; its reservation is retained")
        if r["state"] != "released":
            r["state"] = "released"
            if s["state"] == "waiting":
                s["state"] = "running"
            self.emit("reservation.released", s["id"], reservation=r["id"])
        return {"ok": True}

    def _a_control(self, s: dict, d: dict, now: float, *, source: str | None = None) -> dict:
        sid = s["id"]
        act = d.get("action")
        if act not in {"pause", "resume", "status"}:
            raise ValueError("action must be pause, resume, or status")
        if act == "pause" and s["state"] in {"paused", "draining"}:
            raise ValueError("session is already paused or draining")
        if act == "resume" and s["state"] not in {"paused", "draining"}:
            raise ValueError("session is not paused or draining")
        cid = ident("c")
        c = {"id": cid, "session": sid, "action": act, "state": "pending", "created": now,
             "deadline": now + number(d.get("timeout", 60), "timeout", 1, 86400),
             "reason": text(d.get("reason", "Requested from Arbiter"), "reason")}
        if source:
            c["source"] = source
        self.state["commands"][cid] = c
        if act == "pause":
            s["state"] = "draining"
        if act == "resume":
            self.resume(s, now)
            c["state"] = "acknowledged"
        self.emit("command.requested", sid, **c)
        return c

    def resume(self, s: dict, now: float) -> None:
        sid = s["id"]
        s.pop("auto_paused", None)
        s.pop("preempted_by", None)
        for r in self.state["reservations"].values():
            if r["session"] == sid and r["state"] == "suspended":
                r["state"] = "requested"
                r["created"] = now
        s["state"] = "running"
        self.emit("session.resume_requested", sid)
        for old in self.state["commands"].values():
            if old["session"] == sid and old["action"] == "pause" and old["state"] in {"pending", "delivered", "unanswered"}:
                old["state"] = "cancelled"

    def _a_handoff(self, s, d, now):
        """Exit 75 completed the command. Return its grant to normal queue order."""
        if not s.get("yields") or self.workers_alive(s):
            raise ValueError("A yielding workload must exit before handoff")
        if s["state"] == "draining":
            self._a_checkpoint(s, {"safe": True, "status": "Yielded the bundle; requeued"}, now)
            # Give existing waiters the released capacity before the yielding
            # request competes again, even if the latter has higher priority.
            self.schedule()
            self._a_requeue(s, {}, now)
        elif d.get("reservation"):
            r = self.state["reservations"].get(d["reservation"])
            if not r or r["session"] != s["id"] or r["state"] != "granted":
                raise ValueError("Handoff requires the current grant")
            r["state"] = "suspended"
            self.schedule()
            r.update(state="requested", created=now, reason="Yielded; waiting for scheduler")
            r.pop("promotion_order", None)
            s["state"] = "waiting"
        s["yield_count"] = s.get("yield_count", 0) + 1
        self.emit("session.yielded", s["id"], returncode=75, count=s["yield_count"])
        return {"ok": True}

    def _a_requeue(self, s: dict, d: dict, now: float) -> dict:
        """A yielding job that has checkpointed asks for its bundle back, behind whoever it yielded to."""
        if s["state"] != "paused" or not s.get("yields"):
            raise ValueError("only a paused, yielding job can requeue itself")
        self.resume(s, now)
        return {"ok": True}

    def deliver_command(self, command: dict, now: float) -> None:
        command["state"] = "delivered"
        if command.get("source") == "stale_check":
            command["delivered_at"] = now
            command["deadline"] = now + STALE_CHECK_TIMEOUT_S
        self.emit("command.delivered", command["session"], command=command["id"])

    def _a_delivered(self, s: dict, d: dict, now: float) -> dict:
        c = self.state["commands"].get(d.get("command"))
        if not c or c["session"] != s["id"]:
            raise ValueError("unknown command for session")
        if c["state"] == "pending":
            self.deliver_command(c, now)
        return c

    def _g_promote(self, d: dict, now: float) -> dict:
        rid = text(d.get("reservation"), "reservation ID")
        r = self.state["reservations"].get(rid)
        if r is None:
            raise ValueError("unknown reservation")
        if r["state"] != "requested":
            raise ValueError("only a waiting reservation can be moved to the front")
        # The event cursor is durable and strictly increases across promotions.
        r["promotion_order"] = self.cursor() + 1
        self.emit("reservation.promoted", r["session"], reservation=rid)
        return r

    def _a_checkpoint(self, s: dict, d: dict, now: float) -> dict:
        sid = s["id"]
        if s["state"] != "draining":
            raise ValueError("no pending pause request")
        if d.get("safe") is not True:
            raise ValueError("confirm all resource-using work has stopped with safe=true")
        if s["kind"] == "job" and self.workers_alive(s):
            raise ValueError("Tracked work is still running; its reservation is retained")
        s["status"] = text(d.get("status"), "checkpoint status")
        s["pause_checkpoint"] = s["status"]
        s["state"] = "paused"
        for r in self.state["reservations"].values():
            if r["session"] == sid and r["state"] in {"granted", "requested"}:
                r["state"] = "suspended"
                r.pop("promotion_order", None)
                self.emit("reservation.suspended", sid, reservation=r["id"])
        for c in self.state["commands"].values():
            if c["session"] == sid and c["action"] == "pause" and c["state"] in {"pending", "delivered", "unanswered"}:
                c["state"] = "acknowledged"
        self.emit("session.paused", sid, text=s["status"])
        return {"ok": True, "state": "paused"}

    def _a_finish(self, s: dict, d: dict, now: float) -> dict:
        if d.get("safe") is not True:
            raise ValueError("confirm child work has stopped with safe=true")
        if s.get("groups") and self.workers_alive(s):
            raise ValueError("Tracked work is still running; its reservation is retained")
        code = d.get("returncode")
        if type(code) is int:
            s["signal" if code < 0 else "exit_code"] = abs(code)
        if s.get("cancel_requested") or d.get("cancelled"):
            s["outcome"] = "cancelled"
        elif d.get("oom") is True:
            s["outcome"] = "oom"
        elif d.get("yielded"):
            s["outcome"] = "yielded"
        elif d.get("uncertain"):
            s["outcome"] = "unknown"
        else:
            s["outcome"] = "failure" if d.get("failed") else "success"
        self.end(s, now, failed=bool(d.get("failed")), status=text(d.get("status", "Finished"), "status"))
        return {"ok": True}

    def end(self, s: dict, now: float, failed: bool, status: str, event: str | None = None) -> None:
        sid = s["id"]
        if s["state"] in TERMINAL:
            return
        s["state"] = "failed" if failed else "finished"
        s["ended"] = now
        s.pop("worker_survived", None)
        if s.get("outcome") == "failure" and (s.get("exit_code") or s.get("signal")) and s.get("retries_used", 0) < s.get("retry_limit", 0):
            s["retry_at"] = now + recovery.DELAYS[s["retries_used"]]
            self.emit("recovery.scheduled", sid, at=s["retry_at"])
        s["status"] = status
        for r in self.state["reservations"].values():
            if r["session"] == sid and r["state"] != "released":
                if s["kind"] in PROVIDERS and (r["cpu"] or r["memory_mb"] or r["exclusive"] or r.get("measure") or r.get("disk_gb")):
                    s["last_bundle"] = {key: r[key] for key in ("cpu", "memory_mb", "exclusive", "priority", "label", "measure", "disk_gb", "disk_path") if key in r}
                    if r.get("duration_s"):
                        s["last_bundle"]["eta"] = r["duration_s"]
                r["state"] = "released"
                self.disk_sizes.pop(r["id"], None)
                self.emit("reservation.released", sid, reservation=r["id"])
        for c in self.state["commands"].values():
            if c["session"] == sid and c["state"] in {"pending", "delivered", "unanswered"}:
                c["state"] = "closed"
        self.emit(event or "session." + s["state"], sid, text=status)
        # A hold with no process of its own belongs to the conversation that took it.
        for child in list(self.state["sessions"].values()):
            if child.get("parent") == sid and child["kind"] == "hold" and not child.get("pid"):
                self.end(child, now, failed=False, status=f"Released because {s['name']} ended")

    def _a_message(self, s: dict, d: dict, now: float) -> dict:
        return self.post_message(s["id"], d, now)

    def _g_operator_message(self, d: dict, now: float) -> dict:
        return self.post_message(None, d, now)

    def post_message(self, sender: str | None, d: dict, now: float) -> dict:
        recipient = d.get("to") or None
        if recipient and recipient != "arbiter":
            self.live(recipient)
        reply = d.get("reply_to")
        if reply and reply not in self.state["messages"]:
            raise ValueError("unknown reply target")
        mid = ident("m")
        m = {"id": mid, "from": sender, "to": recipient, "topic": text(d.get("topic", "general"), "topic", 100),
             "text": text(d.get("text"), "message"), "reply_to": reply, "created": now, "acks": []}
        self.state["messages"][mid] = m
        self.emit("message.sent", sender, **m)
        return m

    def _a_ack_message(self, s: dict, d: dict, now: float) -> dict:
        m = self.state["messages"].get(d.get("message"))
        if not m or (m["to"] and m["to"] != s["id"]):
            raise ValueError("unknown message for recipient")
        if s["id"] not in m["acks"]:
            m["acks"].append(s["id"])
            self.emit("message.acknowledged", s["id"], message=m["id"])
        return {"ok": True}

    def _a_approval_request(self, s: dict, d: dict, now: float) -> dict:
        params = d.get("params", {})
        if not isinstance(params, dict) or len(json.dumps(params)) > 30000:
            raise ValueError("params must be a JSON object of at most 30000 characters")
        aid = ident("a")
        approval = {"id": aid, "session": s["id"], "method": text(d.get("method"), "method", 200),
                    "params": params, "state": "pending", "created": now}
        self.state["approvals"][aid] = approval
        self.emit("approval.requested", s["id"], approval=aid, method=approval["method"], params=params)
        if not self.config.error and self.scheduler_switch("auto_approve_inbox"):
            response = operator.inbox_response(approval)
            if response is not None:
                self._a_approval_answer(s, {"approval": aid, "response": response}, now)
                self.emit("approval.auto_approved", s["id"], approval=aid, rule="inbox_read")
        return approval

    def _a_approval_answer(self, s: dict, d: dict, now: float) -> dict:
        a = self.state["approvals"].get(d.get("approval"))
        if not a or a["session"] != s["id"] or a["state"] != "pending":
            raise ValueError("unknown pending approval")
        if not isinstance(d.get("response"), dict):
            raise ValueError("response must be a JSON object matching the provider request")
        a.update(state="answered", response=d["response"])
        self.emit("approval.answered", s["id"], approval=a["id"])
        return {"ok": True}

    def operator_policy(self) -> dict:
        policy = {**self.config["operator"], **self.state.get("operator_settings", {})}
        policy.pop("interval_s", None)  # Legacy file and board settings.
        if policy["provider"] == "command" and not policy["command"]:
            policy["provider"] = ""
        return policy

    def collect_operator_events(self, before: dict) -> None:
        """Queue incoming events in the same transaction as the change that produced them."""
        pending = self.state["operator_events"]
        policy = self.operator_policy()
        enabled = bool(policy["provider"]) and not self.config.error
        replies = enabled and policy["reply_messages"]
        statuses = enabled and policy["review_status"]
        if not replies and not statuses:
            pending.clear()
            return
        for key, event in list(pending.items()):
            if event["action"] == "reply_message":
                session = self.state["sessions"].get(event["session"])
                if not replies or not session or session["state"] in TERMINAL:
                    del pending[key]
            elif not statuses or event["session"] not in self.state["sessions"]:
                del pending[key]

        affected = set()
        for _, kind, sid, body in self.pending:
            if statuses and sid in self.state["sessions"]:
                affected.add(sid)
            if replies and kind == "message.sent":
                message = json.loads(body)
                if not sid or sid == "arbiter":
                    # A human or automated reply supersedes an outstanding response.
                    original = pending.get(message["reply_to"])
                    if original and original["session"] == message["to"]:
                        del pending[message["reply_to"]]
                    continue
                parent = self.state["messages"].get(message["reply_to"], {})
                if message["to"] in (None, "arbiter") and parent.get("from") != "arbiter":
                    pending[message["id"]] = {"action": "reply_message", "session": sid,
                                              "topic": message["topic"], "text": message["text"][:2000]}
        for sid in affected:
            current = self.state["sessions"][sid]
            old = before["sessions"].get(sid)
            if old and (old["state"] != current["state"] or old["status"] != current["status"]
                        or old.get("report_details", {}) != current.get("report_details", {})):
                # Keep the latest update from each session while a review is in flight.
                pending["status/" + sid] = {"action": "flag_status", "session": sid, "change": ident("e"),
                                            "state": current["state"],
                                            "status": current["status"][:1000],
                                            **{k: v[:500] for k, v in current.get("report_details", {}).items()}}

    def preferred_phase_minutes(self) -> int:
        return self.state.get("preferred_phase_minutes", self.config["scheduler"]["preferred_phase_minutes"])

    def scheduler_switch(self, key: str) -> bool:
        return self.state.get("scheduler_settings", {}).get(key, self.config["scheduler"][key])

    def board_settings(self) -> dict:
        policy = self.operator_policy()
        return {**{key: policy[key] for key in OPERATOR_FIELDS},
                **{key: self.scheduler_switch(key) for key in SCHEDULER_SWITCHES},
                "presets": self.state.get("operator_presets", []),
                "preferred_phase_minutes": self.preferred_phase_minutes(), "custom_command": bool(policy["command"]),
                "overridden": any(key in self.state for key in ("operator_settings", "scheduler_settings", "preferred_phase_minutes"))}

    def validate_presets(self, presets):
        if not isinstance(presets, list) or len(presets) > 32:
            raise ValueError("Save up to 32 operator presets")
        names = set()
        for preset in presets:
            if not isinstance(preset, dict) or set(preset) != {"name", "settings"}:
                raise ValueError("Expected a named operator preset")
            name = text(preset["name"], "preset name", 64)
            if not name.isprintable() or name.strip() != name or name.casefold() in names:
                raise ValueError("Give each preset a distinct name")
            names.add(name.casefold())
            fields = preset["settings"]
            if not isinstance(fields, dict) or set(fields) != set(OPERATOR_FIELDS) or not fields["provider"]:
                raise ValueError("A preset needs a provider and its operator settings")
            # Presets reference a locally configured runner by provider name. A runner
            # removed from the file can still be kept in the library until it is restored.
            validate_operator({**fields, "command": ["configured"]})

    def _g_save_settings(self, d: dict, now: float) -> dict:
        before = self.board_settings()
        expected = d.get("expected")
        defaults = {"reply_messages": False, "review_status": False, "instructions": "", "presets": [],
                    **dict.fromkeys(SCHEDULER_SWITCHES, False)}
        if isinstance(expected, dict):
            expected = {**defaults, **{k: v for k, v in expected.items() if k != "interval_s"}}
        if expected != before:
            raise ValueError("Settings changed since you started editing. Reload the current settings and try again.")
        values = d["settings"]
        if isinstance(values, dict):
            values = {**{k: before[k] for k in defaults}, **{k: v for k, v in values.items() if k != "interval_s"}}
        if values is None:
            for key in ("operator_settings", "preferred_phase_minutes", "scheduler_settings"):
                self.state.pop(key, None)
        else:
            if not isinstance(values, dict) or set(values) != {*OPERATOR_FIELDS, *SCHEDULER_SWITCHES, "presets", "preferred_phase_minutes"}:
                raise ValueError("Expected scheduler and operator settings")
            validate_phase_minutes(values["preferred_phase_minutes"])
            operator_fields = {key: values[key] for key in OPERATOR_FIELDS}
            validate_operator({**self.config["operator"], **operator_fields})
            self.validate_presets(values["presets"])
            for key in SCHEDULER_SWITCHES:
                if type(values[key]) is not bool:
                    raise ValueError("Scheduler switches must be boolean")
                if values[key] != before[key]:
                    self.state.setdefault("scheduler_settings", {})[key] = values[key]
            if values["presets"] != before["presets"]:
                self.state["operator_presets"] = values["presets"]
            if any(before[key] != value for key, value in operator_fields.items()):
                self.state["operator_settings"] = operator_fields
            if values["preferred_phase_minutes"] != before["preferred_phase_minutes"]:
                self.state["preferred_phase_minutes"] = values["preferred_phase_minutes"]
        if self.preferred_phase_minutes() != before["preferred_phase_minutes"]:
            self.post_message(None, {"topic": "reservation", "text": phase_guidance(self.preferred_phase_minutes())}, now)
        self.emit("settings.changed")
        return self.board_settings()

    def _g_operator_decision(self, d: dict, now: float) -> dict:
        policy = self.operator_policy()
        choice = d["choice"]
        if self.config.error or not policy["provider"] or policy != d["policy"]:
            raise ValueError("operator configuration changed during review")
        candidate = operator.candidates(self.state, policy, now).get(choice)
        if candidate != d["candidate"] or candidate is None:
            raise ValueError("operator candidate changed during review")

        action, sid = candidate["action"], candidate["session"]
        reason = text(d["reason"], "operator reason", 500)
        if action == "approve_inbox":
            response = operator.inbox_response(self.state["approvals"][choice])
            self._a_approval_answer(self.live(sid), {"approval": choice, "response": response}, now)
        elif action == "promote":
            reservation = self.state["reservations"][choice]
            reservation["priority"] = min(100, reservation["priority"] + 10)
            reservation["operator_promoted"] = True
        elif action == "request_update":
            self._a_control(self.live(sid), {"action": "status", "reason": "Arbiter operator: " + reason}, now)
        elif action == "reply_message":
            self.live(sid)
            self.post_message("arbiter", {"to": sid, "reply_to": choice, "topic": candidate["topic"], "text": reason}, now)
        self.state["operator_events"].pop(choice, None)
        decision = {"action": action, "target": choice, "session": sid, "reason": reason,
                    "provider": policy["provider"], "model": policy["model"], "created": now}
        self.state["operator_decisions"] = (self.state["operator_decisions"] + [decision])[-20:]
        self.emit("operator.decided", sid, **decision)
        return decision

    def _g_operator_events_reviewed(self, d: dict, now: float) -> dict:
        pending = self.state["operator_events"]
        if self.operator_policy() == d["policy"]:
            for key, event in d["events"].items():
                if pending.get(key) == event:
                    del pending[key]
        return {"ok": True}

    def ack(self, sid: str, cid: str, action: str) -> None:
        c = self.state["commands"].get(cid)
        if not c or c["session"] != sid or c["action"] != action:
            raise ValueError("unknown command for session/action")
        c["state"] = "acknowledged"
        self.emit("command.acknowledged", sid, command=cid)

    # -- scheduling --------------------------------------------------------------

    def covers(self, holder: dict, requester: dict) -> bool:
        """A hold covers its own conversation: that conversation's commands use it without waiting."""
        return holder["kind"] == "hold" and self.family(holder) == self.family(requester)

    def outstanding(self, r: dict) -> float:
        """Part of a disk claim not yet written to its path."""
        grown = max(0.0, self.disk_sizes.get(r["id"], r.get("disk_base_gb", 0)) - r.get("disk_base_gb", 0))
        return max(0.0, r["disk_gb"] - grown)

    def disk_status(self, path: str, held: list | None = None) -> dict:
        info = self.probe.disk(path)
        held = held if held is not None else [r for r in self.state["reservations"].values() if r["state"] == "granted"]
        claimed = sum(self.outstanding(r) for r in held if r.get("disk_gb") and r.get("disk_device") == info["device"])
        return {"free_gb": info["free_gb"], "outstanding_gb": claimed, "available_gb": info["free_gb"] - claimed}

    def disk_check(self, path: str, gb: float) -> dict:
        with self.lock:
            status = self.disk_status(path)
            return dict(status, requested_gb=gb, ok=status["available_gb"] >= gb)

    def conflicts(self, r: dict, s: dict, held: list, used: dict, available) -> tuple[list[str], list[dict]]:
        reasons, blockers = [], []
        others = [h for h in held if not self.covers(self.session(h["session"]), s)]
        if self.scheduler_switch("preserve_interactivity"):
            pressure = self.pressure()
            if not pressure["ok"]:
                reasons.append(pressure["reason"])
            if r.get("measure") or "gpu" in r["exclusive"]:
                reasons.extend(self.gpu_pressure)
        for v in r["exclusive"]:
            holders = [h for h in others if v in h["exclusive"]]
            if len(holders) >= self.config.capacity(v):
                reasons.append(f"{v} held by " + ", ".join(self.describe(h) for h in holders))
                blockers += holders
        if r.get("measure") and others:
            reasons.append("Measurement waits for every other reservation to end: " + ", ".join(self.describe(h) for h in others[:3]))
            blockers += others
        measuring = [h for h in others if h.get("measure")]
        if measuring and not r.get("measure"):
            reasons.append(f"{self.describe(measuring[0])} is measuring")
            blockers += measuring
        if used["cpu"] + r["cpu"] > self.capacity["cpu"] + 1e-8:
            reasons.append("CPU budget occupied")
            blockers += [h for h in held if h["cpu"]]
        if used["memory_mb"] + r["memory_mb"] > self.capacity["memory_mb"]:
            reasons.append("Memory budget occupied")
            blockers += [h for h in held if h["memory_mb"]]
        if available is not None and r["memory_mb"] and r["memory_mb"] > max(0, available - self.headroom_mb):
            reasons.append("Insufficient measured memory headroom")
        if r.get("disk_gb"):
            try:
                status = self.disk_status(r["disk_path"], held)
                if r["disk_gb"] > status["available_gb"]:
                    reasons.append(f"Disk: {r['disk_gb']:.0f} GB requested, {max(0, status['available_gb']):.0f} GB free after outstanding claims")
                    if r["disk_gb"] <= status["free_gb"]:
                        # Disk claims only participate in stale checks, never automatic preemption.
                        blockers += [h for h in held if h.get("disk_gb") and h.get("disk_device") == r.get("disk_device")
                                     and self.session(h["session"])["state"] == "stale" and self.outstanding(h) > 0]
            except OSError as e:
                reasons.append(f"Disk: cannot read the filesystem of {r['disk_path']}: {e.strerror}")
        return reasons, blockers

    def short_phase_fits(self, candidate: dict, window: float) -> bool:
        return (0 < candidate.get("duration_s", 0) <= window and not candidate.get("measure")
                and self.session(candidate["session"])["kind"] != "hold")

    def protects_against(self, older: dict, candidate: dict, short_window: float, held: list, used: dict, available) -> bool:
        if candidate.get("measure"):
            return True
        if older.get("measure"):
            return not self.short_phase_fits(candidate, short_window)
        # Set aside the older bundle's share of each resource. Other slots and spare
        # CPU/memory remain usable even if both requests name the same resource.
        requester = self.session(older["session"])
        for name in set(older["exclusive"]).intersection(candidate["exclusive"]):
            occupied = sum(name in h["exclusive"] and not self.covers(self.session(h["session"]), requester) for h in held)
            if occupied + 2 > self.config.capacity(name):
                return True
        for key in ("cpu", "memory_mb"):
            if older[key] and candidate[key] and used[key] + older[key] + candidate[key] > self.capacity[key] + 1e-8:
                return True
        if older["memory_mb"] and candidate["memory_mb"] and available is not None:
            if older["memory_mb"] + candidate["memory_mb"] > available - self.headroom_mb:
                return True
        return bool(older.get("disk_gb") and older.get("disk_device") == candidate.get("disk_device"))

    def backfill_window(self, r: dict, blockers: list, now: float) -> float:
        # Only grants already running when the measurement queued can open this window.
        # Later backfill jobs must drain; they cannot keep admitting one another.
        if not r.get("measure"):
            return 0
        ends = [h.get("eta", 0) for h in blockers if h.get("granted", now) <= r["created"]]
        if not ends:
            return 0
        if min(ends) <= now:  # an original holder has an unknown or overdue ETA
            return SHORT_PHASE_S
        return min(SHORT_PHASE_S, max(ends) - now)

    def check_stale_blockers(self, blockers: dict[str, tuple[dict, dict]], now: float) -> None:
        commands = self.state["commands"]
        for sid, session in self.state["sessions"].items():
            check = session.get("stale_check")
            previous = commands.get(check["command"]) if check else None
            if sid not in blockers:
                if check:
                    check.pop("unanswered_since", None)
                if previous and previous["state"] in {"pending", "delivered", "unanswered"}:
                    previous["state"] = "cancelled"
                    self.emit("command.cancelled", previous["session"], command=previous["id"], reason="Session is no longer a stale blocker")
                continue
            if not check:
                check = session["stale_check"] = {}
            if previous and previous["state"] == "acknowledged":
                check.pop("unanswered_since", None)
            if previous and previous["state"] in {"pending", "delivered", "unanswered"} and now >= previous["deadline"]:
                if "unanswered_since" not in check:
                    check["unanswered_since"] = previous["deadline"]
                    self.emit("session.stale_check_unanswered", sid, command=previous["id"])
            if previous and (previous["state"] == "pending"
                             or now - previous.get("delivered_at", previous["created"]) < STALE_CHECK_COOLDOWN_S):
                continue
            if previous and previous["state"] in {"delivered", "unanswered"}:
                previous["state"] = "cancelled"
                self.emit("command.cancelled", previous["session"], command=previous["id"], reason="Replaced by a new status check")
            waiter, reservation = blockers[sid]
            # When an owner died but its process group survived, its live conversation
            # can answer on the next hook even though the wrapper cannot.
            parent = self.state["sessions"].get(session.get("parent"))
            recipient = parent if (session.get("pid") and parent and parent["kind"] in PROVIDERS
                                   and parent["state"] not in TERMINAL | {"stale"}) else session
            command = self._a_control(recipient, {
                "action": "status", "timeout": STALE_CHECK_TIMEOUT_S,
                "reason": f"{self.who(waiter['session'])} is waiting on {self.describe(reservation)}. "
                          "Report whether this work still needs the reservation.",
            }, now, source="stale_check")
            check["command"] = command["id"]

    def schedule(self) -> None:
        reservations = list(self.state["reservations"].values())
        held = [r for r in reservations if r["state"] == "granted"]
        used = {"cpu": sum(r["cpu"] for r in held), "memory_mb": sum(r["memory_mb"] for r in held)}
        now = time.time()
        available = self.available_mb
        queue = self.waiting_queue()
        # Protected head of queue prevents a stream of small jobs starving a large job.
        protected = []
        blocked = []
        for r in queue:
            s = self.session(r["session"])
            if s["state"] in {"stale", "draining", "paused"} | TERMINAL:
                continue
            reasons, blockers = self.conflicts(r, s, held, used, available)
            window = 0 if r.get("promotion_order") else self.backfill_window(r, blockers, now)
            blocked_on_resources = bool(reasons)
            for older, short_window in protected:
                if self.protects_against(older, r, short_window, held, used, available):
                    ahead = "a promoted request" if older.get("promotion_order") else "an older request"
                    reasons.append(f"Waiting behind {ahead} ({self.who(older['session'])})")
                    break
            if reasons:
                reason = "; ".join(reasons)
                if r["reason"] != reason:
                    r["reason"] = reason
                    self.emit("reservation.waiting", s["id"], reservation=r["id"], reason=reason)
                s["state"] = "waiting"
                if blocked_on_resources and (r.get("promotion_order") or now - r["created"] >= QUEUE_PROTECTION_S):
                    protected.append((r, window))
                blocked.append((r, blockers, window))
                continue
            r["state"] = "granted"
            r.pop("promotion_order", None)
            r["reason"] = "Within configured capacity; no exclusive resource conflicts"
            r["granted"] = now
            if r.get("duration_s"):
                self.set_eta(r, r["duration_s"], now)
            if s.get("yields") and s["state"] == "waiting":
                # A job that just waited its turn needs time to do work before handing back.
                r["yield_after"] = now + min(SHORT_PHASE_S, r.get("duration_s", SHORT_PHASE_S))
            r.pop("handoff_requested", None)
            if r.get("disk_gb"):
                r["disk_base_gb"] = self.probe.size_gb(r["disk_path"])
                self.disk_sizes[r["id"]] = r["disk_base_gb"]
            s["state"] = "running"
            used["cpu"] += r["cpu"]
            used["memory_mb"] += r["memory_mb"]
            if available is not None:
                available -= r["memory_mb"]
            held.append(r)
            self.emit("reservation.granted", s["id"], reservation=r["id"], reason=r["reason"])
        stale_blockers = {}
        # Ask yielding jobs (`arbiter run --preemptible`) to hand over what a waiting request needs.
        for r, blockers, window in blocked:
            for h in blockers:
                owner = self.session(h["session"])
                if owner["state"] == "stale":
                    stale_blockers.setdefault(owner["id"], (r, h))
                    continue
                if (window and h.get("granted", now) > r["created"] and now < h.get("eta", 0)
                        and self.short_phase_fits(h, SHORT_PHASE_S)):
                    continue
                if owner.get("yields") and owner["state"] == "running" and not owner.get("preempted_by") and now >= h.get("yield_after", 0):
                    owner["preempted_by"] = r["session"]
                    self._a_control(owner, {"action": "pause", "timeout": 86400,
                                            "reason": f"{self.who(r['session'])} is waiting for {', '.join(r['exclusive']) or 'the machine'}"}, now)
                elif not owner.get("yields") and not h.get("handoff_requested") and now - h["granted"] >= self.preferred_phase_minutes() * 60:
                    agent = self.session(self.family(owner))
                    if (agent["kind"] in PROVIDERS and agent["state"] not in TERMINAL
                            and self.family(self.session(r["session"])) != agent["id"]):
                        self.post_message(None, {"to": agent["id"], "topic": "reservation",
                            "text": f"{self.who(r['session'])} is waiting on {self.describe(h)}. Finish this phase at the next safe "
                                    "checkpoint, release its resources, and queue the next phase separately."}, now)
                        h["handoff_requested"] = True
        self.check_stale_blockers(stale_blockers, now)

    # -- reads -------------------------------------------------------------------

    def waiting_queue(self) -> list[dict]:
        # All requests age at one priority point per minute. Their relative order
        # depends only on submission time and priority until an operator moves one.
        return sorted((r for r in self.state["reservations"].values() if r["state"] == "requested"),
                      key=lambda r: (-r.get("promotion_order", 0), r["created"] - 60 * r["priority"], r["created"]))

    def cursor(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]

    def wait_for_change(self, cursor: int, timeout: float) -> None:
        """Block until an event newer than `cursor` is committed, or `timeout` seconds pass."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while self.cursor() <= cursor:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.changed.wait(remaining)

    def snapshot(self) -> dict:
        with self.lock:
            state = copy.deepcopy(self.state)
            for position, r in enumerate(self.waiting_queue(), 1):
                state["reservations"][r["id"]]["queue_position"] = position
            for s in state["sessions"].values():
                s.pop("token", None)
                if s["state"] in TERMINAL and s["kind"] in PROVIDERS | {"job"} and not s.get("successor"):
                    s["recovery_reason"] = recovery.unavailable(self, s, inspect_files=False)
                    s["confirm_unknown"] = s.get("outcome", "unknown") == "unknown"
            state["capacity"] = self.capacity.copy()
            state["cursor"] = self.cursor()
            state["pressure"] = self.pressure()
            state["resources"] = {k: {"capacity": v.get("capacity", 1), "match": v.get("match")} for k, v in self.config["resource"].items()}
            state["config_error"] = self.config.error
            state.pop("operator_presets", None)
            state.pop("scheduler_settings", None)
            state.pop("operator_settings", None)
            state.pop("operator_events", None)
            state.pop("preferred_phase_minutes", None)
            state["settings"] = self.board_settings()
            state["operator"] = {key: state["settings"][key] for key in OPERATOR_FIELDS}
            return state

    def events(self, after: int = 0, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id,at,kind,session,body FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
            return [{"id": r[0], "at": r[1], "kind": r[2], "session": r[3], "data": json.loads(r[4])} for r in rows]

    def inbox(self, sid: str) -> dict:
        with self.lock:
            self.session(sid)
            return copy.deepcopy({
                "commands": [c for c in self.state["commands"].values() if c["session"] == sid and c["state"] in {"pending", "delivered", "unanswered"}],
                "messages": [m for m in self.state["messages"].values() if m["from"] != sid and m["to"] in {None, sid} and sid not in m["acks"]]})
