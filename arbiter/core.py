"""Transactional state machine. No network or agent-specific policy lives here."""
import copy
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

TERMINAL = {"finished", "failed"}


def ident(prefix):
    return prefix + "_" + uuid.uuid4().hex[:12]


def number(value, name, low=0, high=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def text(value, name, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text, at most {limit} characters")
    return value


class Store:
    def __init__(self, path, cpu=4, memory_mb=4096, stale_seconds=45, headroom_mb=512):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, session TEXT, body TEXT)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {"sessions": {}, "reservations": {}, "commands": {}, "messages": {}}
        self.state.setdefault("approvals", {})
        self.capacity = {"cpu": number(cpu, "cpu", .01), "memory_mb": number(memory_mb, "memory_mb", 1)}
        self.stale_seconds = stale_seconds
        self.headroom_mb = headroom_mb
        self.available_mb = None
        self.recovered_since = None
        self.pending = []
        # A restarted daemon cannot infer that old owners stopped using resources.
        self.apply("reconcile", {})

    def emit(self, event_type, actor=None, **body):
        self.pending.append((time.time(), event_type, actor, json.dumps(body)))

    def session(self, sid):
        if sid not in self.state["sessions"]:
            raise ValueError("unknown session")
        return self.state["sessions"][sid]

    def live(self, sid):
        s = self.session(sid)
        if s["state"] in TERMINAL:
            raise ValueError("session has finished")
        return s

    def apply(self, action, data):
        with self.changed:
            before = copy.deepcopy(self.state)
            self.pending = []
            try:
                result = self.mutate(action, data)
                self.schedule()
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.state),))
                    self.db.executemany("INSERT INTO events(at,kind,session,body) VALUES (?,?,?,?)", self.pending)
                self.changed.notify_all()
                return copy.deepcopy(result)
            except Exception:
                self.state = before
                raise
            finally:
                self.pending = []

    def mutate(self, action, d):
        now = time.time()
        if action == "reconcile":
            for s in self.state["sessions"].values():
                if s["state"] not in TERMINAL and s["state"] != "stale":
                    s["previous_state"] = s["state"]
                    s["state"] = "stale"
                    self.emit("session.stale", s["id"], reason="daemon restarted; reservations retained")
            return {"ok": True}
        if action == "register":
            sid = ident("s")
            kind = d.get("kind", "external")
            if kind not in {"external", "codex", "claude", "job", "demo"}:
                raise ValueError("unknown session kind")
            s = {"id": sid, "name": text(d.get("name"), "name", 100), "kind": kind,
                 "state": "running", "created": now, "heartbeat": now, "status": "Registered",
                 "cwd": d.get("cwd", ""), "preemptible": bool(d.get("preemptible", False)),
                 "capabilities": d.get("capabilities", []), "token": secrets.token_urlsafe(32)}
            self.state["sessions"][sid] = s
            self.emit("session.started", sid, name=s["name"], kind=kind)
            return s
        if action == "tick":
            self.available_mb = d.get("available_mb", self.available_mb)
            for s in self.state["sessions"].values():
                if s["state"] not in TERMINAL | {"stale"} and now - s["heartbeat"] > self.stale_seconds:
                    s["previous_state"] = s["state"]
                    s["state"] = "stale"
                    self.emit("session.stale", s["id"], reason="heartbeat expired; reservations retained")
            for c in self.state["commands"].values():
                if c["state"] in {"pending", "delivered"} and now > c["deadline"]:
                    c["state"] = "unanswered"
                    self.emit("command.unanswered", c["session"], command=c["id"], action=c["action"])
            if self.available_mb is not None:
                if self.available_mb < self.headroom_mb:
                    self.recovered_since = None
                    # One draining worker at a time; a pause request is not a resource release.
                    if not any(s["state"] == "draining" for s in self.state["sessions"].values()):
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
            return {"ok": True}
        sid = d.get("session")
        s = self.live(sid)
        if action == "heartbeat":
            s["heartbeat"] = now
            if s["state"] == "stale":
                s["state"] = s.pop("previous_state", "running")
                self.emit("session.reconnected", sid, state=s["state"])
            return {"ok": True}
        if action == "report":
            s["status"] = text(d.get("status"), "status")
            self.emit("session.status", sid, text=s["status"], progress=d.get("progress"), blocker=d.get("blocker"), stopping_point=d.get("stopping_point"))
            cid = d.get("command")
            if cid:
                self.ack(sid, cid, "status")
            return {"ok": True}
        if action == "reserve":
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
        if action == "release":
            r = self.state["reservations"].get(d.get("reservation"))
            if not r or r["session"] != sid:
                raise ValueError("unknown reservation for session")
            if r["state"] not in {"released", "cancelled"}:
                r["state"] = "released"
                if s["state"] == "waiting":
                    s["state"] = "running"
                self.emit("reservation.released", sid, reservation=r["id"])
            return {"ok": True}
        if action == "control":
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
        if action == "delivered":
            c = self.state["commands"].get(d.get("command"))
            if not c or c["session"] != sid:
                raise ValueError("unknown command for session")
            if c["state"] == "pending":
                c["state"] = "delivered"
                self.emit("command.delivered", sid, command=c["id"])
            return c
        if action == "checkpoint":
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
        if action == "finish":
            if d.get("safe") is not True:
                raise ValueError("confirm child work has stopped with safe=true")
            s["state"] = "failed" if d.get("failed") else "finished"
            s["status"] = text(d.get("status", "Finished"), "status")
            for r in self.state["reservations"].values():
                if r["session"] == sid and r["state"] not in {"released", "cancelled"}:
                    r["state"] = "released"
                    self.emit("reservation.released", sid, reservation=r["id"])
            for c in self.state["commands"].values():
                if c["session"] == sid and c["state"] in {"pending", "delivered", "unanswered"}:
                    c["state"] = "closed"
            self.emit("session." + s["state"], sid, text=s["status"])
            return {"ok": True}
        if action == "message":
            recipient = d.get("to")
            if recipient:
                self.live(recipient)
            reply = d.get("reply_to")
            if reply and reply not in self.state["messages"]:
                raise ValueError("unknown reply target")
            mid = ident("m")
            m = {"id": mid, "from": sid, "to": recipient, "topic": text(d.get("topic", "general"), "topic", 100),
                 "text": text(d.get("text"), "message"), "reply_to": reply, "created": now, "acks": []}
            self.state["messages"][mid] = m
            self.emit("message.sent", sid, **m)
            return m
        if action == "ack_message":
            m = self.state["messages"].get(d.get("message"))
            if not m or (m["to"] and m["to"] != sid):
                raise ValueError("unknown message for recipient")
            if sid not in m["acks"]:
                m["acks"].append(sid)
                self.emit("message.acknowledged", sid, message=m["id"])
            return {"ok": True}
        if action == "approval_request":
            aid = ident("a")
            approval = {"id": aid, "session": sid, "method": text(d.get("method"), "method", 200),
                        "params": d.get("params", {}), "state": "pending", "created": now}
            self.state["approvals"][aid] = approval
            self.emit("approval.requested", sid, approval=aid, method=approval["method"], params=approval["params"])
            return approval
        if action == "approval_answer":
            a = self.state["approvals"].get(d.get("approval"))
            if not a or a["session"] != sid or a["state"] != "pending":
                raise ValueError("unknown pending approval")
            if not isinstance(d.get("response"), dict):
                raise ValueError("response must be a JSON object matching the provider request")
            a.update(state="answered", response=d["response"])
            self.emit("approval.answered", sid, approval=a["id"])
            return {"ok": True}
        raise ValueError("unknown action")

    def ack(self, sid, cid, action):
        c = self.state["commands"].get(cid)
        if not c or c["session"] != sid or c["action"] != action:
            raise ValueError("unknown command for session/action")
        c["state"] = "acknowledged"
        self.emit("command.acknowledged", sid, command=cid)

    def schedule(self):
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

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            for s in state["sessions"].values():
                s.pop("token", None)
            state["capacity"] = self.capacity.copy()
            state["cursor"] = self.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            return state

    def events(self, after=0, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT id,at,kind,session,body FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
            return [{"id": r[0], "at": r[1], "kind": r[2], "session": r[3], "data": json.loads(r[4])} for r in rows]

    def inbox(self, sid):
        with self.lock:
            self.session(sid)
            return copy.deepcopy({
                "commands": [c for c in self.state["commands"].values() if c["session"] == sid and c["state"] in {"pending", "delivered", "unanswered"}],
                "messages": [m for m in self.state["messages"].values() if m["from"] != sid and m["to"] in {None, sid} and sid not in m["acks"]]})
