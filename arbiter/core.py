"""Transactional state machine. No network or agent-specific policy lives here."""
from __future__ import annotations

import copy
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TERMINAL = {"finished", "failed"}
SCHEMA_VERSION = 1
PRUNE_INTERVAL = 600


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


class Store:
    def __init__(self, path, cpu: float = 4, memory_mb: float = 4096, stale_seconds: float = 45,
                 headroom_mb: float = 512, retention_days: float | None = None):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL with NORMAL cannot corrupt the database; it may lose the last commit on power loss.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, session TEXT, body TEXT)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {}
        self.migrate()
        self.capacity = {"cpu": number(cpu, "cpu", .01), "memory_mb": number(memory_mb, "memory_mb", 1)}
        self.stale_seconds = stale_seconds
        self.headroom_mb = headroom_mb
        self.retention_days = retention_days
        self.available_mb: float | None = None
        self.recovered_since: float | None = None
        self.pending: list = []
        self.prune_events_before: float | None = None
        self.last_prune = time.monotonic()
        # Staleness uses a monotonic clock that is never persisted: a wall-clock jump must not
        # expire every session at once, and a restart marks all sessions stale regardless.
        self.beats: dict[str, float] = {}
        # A restarted daemon cannot infer that old owners stopped using resources.
        self.apply("reconcile", {})

    def migrate(self) -> None:
        """Bring a loaded state blob up to SCHEMA_VERSION. Add a step per schema change."""
        state = self.state
        for key in ("sessions", "reservations", "commands", "messages", "approvals"):
            state.setdefault(key, {})
        version = state.get("schema_version", 0)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"State was written by a newer Arbiter (schema {version}); refusing to downgrade it")
        # 0 -> 1: introduced schema_version; reservations in the removed "cancelled" state are released.
        for r in state["reservations"].values():
            if r["state"] == "cancelled":
                r["state"] = "released"
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
            saved = (self.available_mb, self.recovered_since)
            self.pending = []
            self.prune_events_before = None
            try:
                result = self.mutate(action, data)
                self.schedule()
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.state),))
                    self.db.executemany("INSERT INTO events(at,kind,session,body) VALUES (?,?,?,?)", self.pending)
                    if self.prune_events_before is not None:
                        # Keep the newest event so the cursor never moves backwards.
                        self.db.execute("DELETE FROM events WHERE at<? AND id<(SELECT MAX(id) FROM events)", (self.prune_events_before,))
                self.changed.notify_all()
                return copy.deepcopy(result)
            except Exception:
                self.state = before
                self.available_mb, self.recovered_since = saved
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
        for s in self.state["sessions"].values():
            if s["state"] not in TERMINAL and s["state"] != "stale":
                s["previous_state"] = s["state"]
                s["state"] = "stale"
                self.emit("session.stale", s["id"], reason="daemon restarted; reservations retained")
        return {"ok": True}

    def _g_register(self, d: dict, now: float) -> dict:
        sid = ident("s")
        kind = d.get("kind", "external")
        if kind not in {"external", "codex", "claude", "job", "demo"}:
            raise ValueError("unknown session kind")
        cwd = d.get("cwd", "")
        if not isinstance(cwd, str) or len(cwd) > 1000:
            raise ValueError("cwd must be text, at most 1000 characters")
        capabilities = d.get("capabilities", [])
        if not isinstance(capabilities, list) or len(capabilities) > 32:
            raise ValueError("capabilities must be a list with at most 32 entries")
        s = {"id": sid, "name": text(d.get("name"), "name", 100), "kind": kind,
             "state": "running", "created": now, "heartbeat": now, "status": "Registered",
             "cwd": cwd, "preemptible": bool(d.get("preemptible", False)),
             "capabilities": [text(c, "capability", 100) for c in capabilities], "token": secrets.token_urlsafe(32)}
        self.state["sessions"][sid] = s
        self.beats[sid] = time.monotonic()
        self.emit("session.started", sid, name=s["name"], kind=kind)
        return s

    def _g_tick(self, d: dict, now: float) -> dict:
        self.available_mb = d.get("available_mb", self.available_mb)
        mono = time.monotonic()
        for s in self.state["sessions"].values():
            if s["state"] in TERMINAL | {"stale"}:
                continue
            if mono - self.beats.setdefault(s["id"], mono) > self.stale_seconds:
                s["previous_state"] = s["state"]
                s["state"] = "stale"
                self.emit("session.stale", s["id"], reason="heartbeat expired; reservations retained")
        for c in self.state["commands"].values():
            if c["state"] in {"pending", "delivered"} and now > c["deadline"]:
                c["state"] = "unanswered"
                self.emit("command.unanswered", c["session"], command=c["id"], action=c["action"])
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
        state = self.state
        counts = {"sessions": len(old)}
        for key, owner in (("reservations", lambda v: v["session"]), ("commands", lambda v: v["session"]),
                           ("approvals", lambda v: v["session"]), ("messages", lambda v: v["from"])):
            doomed = [k for k, v in state[key].items() if owner(v) in old or (key == "messages" and v["to"] in old)]
            for k in doomed:
                del state[key][k]
            counts[key] = len(doomed)
        for sid in old:
            del state["sessions"][sid]
            self.beats.pop(sid, None)
        if old:
            self.emit("state.pruned", None, **counts)
        self.prune_events_before = cutoff

    def _a_heartbeat(self, s: dict, d: dict, now: float) -> dict:
        s["heartbeat"] = now
        self.beats[s["id"]] = time.monotonic()
        if s["state"] == "stale":
            s["state"] = s.pop("previous_state", "running")
            self.emit("session.reconnected", s["id"], state=s["state"])
        return {"ok": True}

    def _a_report(self, s: dict, d: dict, now: float) -> dict:
        s["status"] = text(d.get("status"), "status")
        self.emit("session.status", s["id"], text=s["status"], progress=optional_text(d.get("progress"), "progress"),
                  blocker=optional_text(d.get("blocker"), "blocker"), stopping_point=optional_text(d.get("stopping_point"), "stopping_point"))
        cid = d.get("command")
        if cid:
            self.ack(s["id"], cid, "status")
        return {"ok": True}

    def _a_reserve(self, s: dict, d: dict, now: float) -> dict:
        sid = s["id"]
        if s["state"] in {"draining", "paused", "stale"}:
            raise ValueError("cannot request resources while draining, paused, or stale")
        cpu = number(d.get("cpu", 0), "cpu")
        mem = number(d.get("memory_mb", 0), "memory_mb")
        exclusive = d.get("exclusive", [])
        if not isinstance(exclusive, list) or len(exclusive) > 64:
            raise ValueError("exclusive must be a list with at most 64 names")
        exclusive = sorted(set(text(v, "resource", 250) for v in exclusive))
        if not cpu and not mem and not exclusive:
            raise ValueError("request at least one resource")
        if cpu > self.capacity["cpu"] or mem > self.capacity["memory_mb"]:
            raise ValueError("request exceeds configured capacity")
        # One atomic bundle per session avoids hold-and-wait deadlocks.
        if any(r["session"] == sid and r["state"] in {"requested", "granted", "suspended"} for r in self.state["reservations"].values()):
            raise ValueError("release existing resource bundle before requesting another")
        rid = ident("r")
        r = {"id": rid, "session": sid, "cpu": cpu, "memory_mb": mem, "exclusive": exclusive,
             "priority": number(d.get("priority", 0), "priority", -100, 100), "created": now,
             "state": "requested", "reason": "Waiting for scheduler"}
        self.state["reservations"][rid] = r
        self.emit("reservation.requested", sid, reservation=rid, cpu=cpu, memory_mb=mem, exclusive=exclusive)
        return r

    def _a_release(self, s: dict, d: dict, now: float) -> dict:
        r = self.state["reservations"].get(d.get("reservation"))
        if not r or r["session"] != s["id"]:
            raise ValueError("unknown reservation for session")
        if r["state"] != "released":
            r["state"] = "released"
            if s["state"] == "waiting":
                s["state"] = "running"
            self.emit("reservation.released", s["id"], reservation=r["id"])
        return {"ok": True}

    def _a_control(self, s: dict, d: dict, now: float) -> dict:
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
        self.state["commands"][cid] = c
        if act == "pause":
            s["state"] = "draining"
        if act == "resume":
            s.pop("auto_paused", None)
            for r in self.state["reservations"].values():
                if r["session"] == sid and r["state"] == "suspended":
                    r["state"] = "requested"
                    r["created"] = now
            s["state"] = "running"
            c["state"] = "acknowledged"
            self.emit("session.resume_requested", sid)
            for old in self.state["commands"].values():
                if old["session"] == sid and old["action"] == "pause" and old["state"] in {"pending", "delivered", "unanswered"}:
                    old["state"] = "cancelled"
        self.emit("command.requested", sid, **c)
        return c

    def _a_delivered(self, s: dict, d: dict, now: float) -> dict:
        c = self.state["commands"].get(d.get("command"))
        if not c or c["session"] != s["id"]:
            raise ValueError("unknown command for session")
        if c["state"] == "pending":
            c["state"] = "delivered"
            self.emit("command.delivered", s["id"], command=c["id"])
        return c

    def _a_checkpoint(self, s: dict, d: dict, now: float) -> dict:
        sid = s["id"]
        if s["state"] != "draining":
            raise ValueError("no pending pause request")
        if d.get("safe") is not True:
            raise ValueError("confirm all resource-using work has stopped with safe=true")
        s["status"] = text(d.get("status"), "checkpoint status")
        s["state"] = "paused"
        for r in self.state["reservations"].values():
            if r["session"] == sid and r["state"] in {"granted", "requested"}:
                r["state"] = "suspended"
                self.emit("reservation.suspended", sid, reservation=r["id"])
        for c in self.state["commands"].values():
            if c["session"] == sid and c["action"] == "pause" and c["state"] in {"pending", "delivered", "unanswered"}:
                c["state"] = "acknowledged"
        self.emit("session.paused", sid, text=s["status"])
        return {"ok": True, "state": "paused"}

    def _a_finish(self, s: dict, d: dict, now: float) -> dict:
        sid = s["id"]
        if d.get("safe") is not True:
            raise ValueError("confirm child work has stopped with safe=true")
        s["state"] = "failed" if d.get("failed") else "finished"
        s["ended"] = now
        s["status"] = text(d.get("status", "Finished"), "status")
        for r in self.state["reservations"].values():
            if r["session"] == sid and r["state"] != "released":
                r["state"] = "released"
                self.emit("reservation.released", sid, reservation=r["id"])
        for c in self.state["commands"].values():
            if c["session"] == sid and c["state"] in {"pending", "delivered", "unanswered"}:
                c["state"] = "closed"
        self.emit("session." + s["state"], sid, text=s["status"])
        return {"ok": True}

    def _a_message(self, s: dict, d: dict, now: float) -> dict:
        recipient = d.get("to")
        if recipient:
            self.live(recipient)
        reply = d.get("reply_to")
        if reply and reply not in self.state["messages"]:
            raise ValueError("unknown reply target")
        mid = ident("m")
        m = {"id": mid, "from": s["id"], "to": recipient, "topic": text(d.get("topic", "general"), "topic", 100),
             "text": text(d.get("text"), "message"), "reply_to": reply, "created": now, "acks": []}
        self.state["messages"][mid] = m
        self.emit("message.sent", s["id"], **m)
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

    def ack(self, sid: str, cid: str, action: str) -> None:
        c = self.state["commands"].get(cid)
        if not c or c["session"] != sid or c["action"] != action:
            raise ValueError("unknown command for session/action")
        c["state"] = "acknowledged"
        self.emit("command.acknowledged", sid, command=cid)

    def schedule(self) -> None:
        reservations = list(self.state["reservations"].values())
        held = [r for r in reservations if r["state"] == "granted"]
        used_cpu = sum(r["cpu"] for r in held)
        used_mem = sum(r["memory_mb"] for r in held)
        locked = {v: r["session"] for r in held for v in r["exclusive"]}
        now = time.time()
        available = self.available_mb
        queue = sorted((r for r in reservations if r["state"] == "requested"),
                       key=lambda r: (-(r["priority"] + (now-r["created"])/60), r["created"]))
        # Protected head of queue prevents a stream of small jobs starving a large job.
        barrier = False
        for r in queue:
            s = self.session(r["session"])
            if s["state"] in {"stale", "draining", "paused"} | TERMINAL:
                continue
            conflicts = [f"{v} held by {locked[v]}" for v in r["exclusive"] if v in locked]
            if used_cpu + r["cpu"] > self.capacity["cpu"] + 1e-8:
                conflicts.append("CPU budget occupied")
            if used_mem + r["memory_mb"] > self.capacity["memory_mb"]:
                conflicts.append("Memory budget occupied")
            if available is not None and r["memory_mb"] and r["memory_mb"] > max(0, available - self.headroom_mb):
                conflicts.append("Insufficient measured memory headroom")
            if barrier:
                conflicts.append("Waiting behind an older request")
            if conflicts:
                reason = "; ".join(conflicts)
                if r["reason"] != reason:
                    r["reason"] = reason
                    self.emit("reservation.waiting", s["id"], reservation=r["id"], reason=reason)
                s["state"] = "waiting"
                barrier = barrier or now - r["created"] >= 30
                continue
            r["state"] = "granted"
            r["reason"] = "Within configured capacity; no exclusive resource conflicts"
            s["state"] = "running"
            used_cpu += r["cpu"]
            used_mem += r["memory_mb"]
            if available is not None:
                available -= r["memory_mb"]
            locked.update({v: r["session"] for v in r["exclusive"]})
            self.emit("reservation.granted", s["id"], reservation=r["id"], reason=r["reason"])

    # -- reads -------------------------------------------------------------------

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
            for s in state["sessions"].values():
                s.pop("token", None)
            state["capacity"] = self.capacity.copy()
            state["cursor"] = self.cursor()
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
