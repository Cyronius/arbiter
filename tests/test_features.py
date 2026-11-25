"""Owner liveness, holds and ETAs, families, capacity, measurement, yielding, disk claims, attach and the guard."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from arbiter import guard
from arbiter.config import Config
from arbiter.core import Store
from arbiter.hook import Hook, inbox_text
from arbiter.server import Launcher, permitted
from arbiter.workers import duration


class FakeProbe:
    def __init__(self):
        self.pids, self.groups = {100, 200, 300}, set()
        self.free_gb, self.sizes = 100.0, {}

    def alive(self, pid, started=None):
        return pid in self.pids

    def start_time(self, pid):
        return 1 if pid in self.pids else None

    def group_alive(self, pgid, started=None):
        current = self.start_time(pgid)
        return pgid in self.groups and (started is None or current is None or started == current)

    def disk(self, path):
        return {"device": 1, "free_gb": self.free_gb}

    def size_gb(self, path):
        return self.sizes.get(path, 0.0)


CONFIG = {"resource": {"gpu": {"match": r"\bllama-server\b"}, "network": {"capacity": 2}},
          "guard": {"wait_inline_s": 0, "cpu_busy_pct": 90}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.probe = FakeProbe()
        self.store = Store(self.path, cpu=4, memory_mb=4096, headroom_mb=100, probe=self.probe, config=Config(data=CONFIG))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def register(self, name="w", **extra):
        return self.store.apply("register", {"name": name, **extra})["id"]

    def apply(self, op, sid, **extra):
        return self.store.apply(op, {"session": sid, **extra})

    def state(self, sid=None):
        snap = self.store.snapshot()
        return snap["sessions"][sid] if sid else snap

    def reservation(self, rid):
        return self.store.snapshot()["reservations"][rid]

    def attach(self, external="conv-1", pid=100, provider="claude", **extra):
        return self.store.apply("attach", {"provider": provider, "external_id": external, "cwd": "/home/u/proj", "pid": pid, **extra})["id"]


class LivenessTests(Base):
    def test_dead_owner_without_children_is_reaped_and_releases(self):
        a = self.register(kind="job", pid=200)
        r = self.apply("reserve", a, exclusive=["gpu"])
        b = self.register()
        rb = self.apply("reserve", b, exclusive=["gpu"])
        self.probe.pids.discard(200)
        self.store.apply("tick", {})
        self.assertEqual(self.state(a)["state"], "failed")
        self.assertEqual(self.reservation(r["id"])["state"], "released")
        self.assertEqual(self.reservation(rb["id"])["state"], "granted")
        self.assertTrue(any(e["kind"] == "session.reaped" for e in self.store.events()))

    def test_dead_owner_with_running_command_is_held_until_it_exits(self):
        a = self.register(kind="job", pid=200)
        r = self.apply("reserve", a, exclusive=["gpu"])
        self.apply("track", a, pgid=555)
        self.probe.groups.add(555)
        self.probe.pids.discard(200)
        self.store.apply("tick", {})
        self.assertEqual(self.state(a)["state"], "stale")
        self.assertEqual(self.reservation(r["id"])["state"], "granted")
        self.probe.groups.clear()
        self.store.apply("tick", {})
        self.assertEqual(self.reservation(r["id"])["state"], "released")

    def test_stale_job_blocker_check_goes_to_its_live_conversation(self):
        parent = self.attach()
        holder = self.register(kind="job", pid=200, parent=parent)
        held = self.apply("reserve", holder, exclusive=["gpu"])
        self.apply("track", holder, pgid=555)
        self.probe.groups.add(555)
        waiting = self.apply("reserve", self.register(), exclusive=["gpu"])
        self.probe.pids.discard(200)
        self.store.apply("tick", {})
        self.assertFalse(self.store.inbox(holder)["commands"])
        check, = self.store.inbox(parent)["commands"]
        self.assertEqual(check["source"], "stale_check")
        self.apply("report", parent, status="The child still needs the GPU", command=check["id"])
        self.assertEqual(self.state(holder)["state"], "stale")
        self.assertEqual(self.reservation(held["id"])["state"], "granted")
        self.probe.groups.clear()
        self.store.apply("tick", {})
        self.assertEqual(self.reservation(waiting["id"])["state"], "granted")

    def test_stale_disk_claim_checks_only_when_the_claim_blocks_available_space(self):
        holder, waiter = self.register(), self.register()
        self.apply("reserve", holder, disk_gb=70, disk_path=str(self.path.parent / "models"))
        self.store.beats[holder] -= 100
        self.store.apply("tick", {})
        self.probe.free_gb = 35
        self.apply("reserve", waiter, disk_gb=40, disk_path=str(self.path.parent / "other"))
        self.assertFalse(self.store.inbox(holder)["commands"])
        self.probe.free_gb = 100
        self.store.apply("tick", {})
        check, = self.store.inbox(holder)["commands"]
        self.assertEqual(check["source"], "stale_check")

    def test_live_owner_survives_restart_without_heartbeats(self):
        a = self.register(kind="job", pid=200)
        self.apply("reserve", a, exclusive=["gpu"])
        self.store.db.close()
        self.store = Store(self.path, probe=self.probe, config=Config(data=CONFIG))
        self.assertEqual(self.state(a)["state"], "running")
        self.store.beats[a] -= 1000
        self.store.apply("tick", {})
        self.assertEqual(self.state(a)["state"], "running")

    def test_registering_a_dead_owner_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not running"):
            self.register(pid=999)


class HoldTests(Base):
    def test_unattended_hold_expires_after_its_grace_and_extend_pushes_it(self):
        h = self.register(kind="hold")
        r = self.apply("reserve", h, exclusive=["gpu"], eta=60)
        self.assertGreater(r["stale_at"], r["eta"])
        self.store.state["reservations"][r["id"]]["stale_at"] = time.time() - 1
        self.apply("extend", h, eta=600)
        self.store.apply("tick", {})
        self.assertEqual(self.state(h)["state"], "running")
        self.store.state["reservations"][r["id"]]["stale_at"] = time.time() - 1
        self.store.apply("tick", {})
        self.assertEqual(self.state(h)["state"], "finished")
        self.assertTrue(any(e["kind"] == "session.expired" for e in self.store.events()))

    def test_hold_covers_its_own_conversation_but_jobs_still_take_turns(self):
        agent = self.attach()
        other = self.attach("conv-2", pid=300)
        h = self.register(kind="hold", parent=agent)
        self.apply("reserve", h, exclusive=["gpu"], eta=600)
        mine = self.register(kind="job", parent=agent)
        self.assertEqual(self.apply("reserve", mine, exclusive=["gpu"])["state"], "granted")
        sibling = self.register(kind="job", parent=agent)
        self.assertEqual(self.apply("reserve", sibling, exclusive=["gpu"])["state"], "requested")
        theirs = self.register(kind="job", parent=other)
        self.assertEqual(self.apply("reserve", theirs, exclusive=["gpu"])["state"], "requested")

    def test_hold_without_pid_ends_with_its_conversation(self):
        agent = self.attach()
        h = self.register(kind="hold", parent=agent)
        self.apply("reserve", h, exclusive=["gpu"], eta=600)
        self.store.apply("hook", {"provider": "claude", "external_id": "conv-1", "pid": 100, "event": "end"})
        self.assertEqual(self.state(h)["state"], "finished")

    def test_adopt_hands_a_hold_to_another_process(self):
        h = self.register(kind="hold", pid=200)
        self.apply("reserve", h, exclusive=["gpu"], eta=600)
        self.apply("adopt", h, pid=300)
        self.probe.pids.discard(200)
        self.store.apply("tick", {})
        self.assertEqual(self.state(h)["state"], "running")
        self.apply("adopt", h, pid=None)
        self.assertNotIn("pid", self.state(h))


class SchedulingTests(Base):
    def test_aged_gpu_request_allows_unrelated_work(self):
        owner, waiter, short = self.register(), self.register(), self.register()
        self.apply("reserve", owner, exclusive=["gpu"])
        self.apply("reserve", waiter, exclusive=["gpu"])
        with patch("arbiter.core.time.time", return_value=time.time() + 40):
            self.assertEqual(self.apply("reserve", short, exclusive=["npu"])["state"], "granted")

    def test_protection_leaves_unused_capacity_available(self):
        owner, waiter = self.register(), self.register()
        self.apply("reserve", owner, exclusive=["gpu"])
        self.apply("reserve", waiter, exclusive=["network", "gpu"], cpu=1)
        with patch("arbiter.core.time.time", return_value=time.time() + 40):
            self.assertEqual(self.apply("reserve", self.register(), exclusive=["network"], cpu=3)["state"], "granted")
            self.assertEqual(self.apply("reserve", self.register(), exclusive=["network"])["state"], "requested")
            self.assertEqual(self.apply("reserve", self.register(), cpu=1)["state"], "requested")
            self.apply("finish", owner, safe=True)
            self.assertEqual(self.state(waiter)["state"], "running")

    def test_short_phases_backfill_then_drain_for_measurement(self):
        owner, bench, short, later = [self.register() for _ in range(4)]
        self.apply("reserve", owner, exclusive=["gpu"], eta=600)
        rm = self.apply("reserve", bench, measure=True, eta=60)
        with patch("arbiter.core.time.time", return_value=time.time() + 40):
            self.assertEqual(self.apply("reserve", self.register(), cpu=1, eta=590)["state"], "requested")
            self.assertEqual(self.apply("reserve", short, exclusive=["npu"], eta=300)["state"], "granted")
            self.apply("finish", owner, safe=True)
            self.assertEqual(self.apply("reserve", later, exclusive=["gpu"], eta=30)["state"], "requested")
            self.apply("finish", short, safe=True)
            granted = self.reservation(rm["id"])
            self.assertEqual(granted["state"], "granted")
            self.assertEqual(granted["eta"] - granted["granted"], 60)
            self.assertEqual(self.state(later)["state"], "waiting")
            self.apply("finish", bench, safe=True)
            self.assertEqual(self.state(later)["state"], "running")

    def test_overdue_holder_gets_one_handoff_note_and_short_work_can_run(self):
        agent = self.attach()
        owner = self.register(kind="job", parent=agent)
        r = self.apply("reserve", owner, exclusive=["gpu"], eta=60)
        self.apply("reserve", self.register("bench"), measure=True)
        with patch("arbiter.core.time.time", return_value=time.time() + 700):
            self.assertEqual(self.apply("reserve", self.register(), cpu=1, eta=601)["state"], "requested")
            self.assertEqual(self.apply("reserve", self.register(), exclusive=["npu"], eta=600)["state"], "granted")
            self.assertEqual(self.reservation(r["id"])["state"], "granted")
            notes = self.store.inbox(agent)["messages"]
            self.assertEqual(len(notes), 1)
            self.assertIn("bench is waiting", notes[0]["text"])

    def test_two_yielding_jobs_get_work_time_after_handoff(self):
        first, next_job = [self.register(kind="job", yields=True) for _ in range(2)]
        self.apply("reserve", first, cpu=4)
        self.apply("reserve", next_job, cpu=4, eta=60)
        self.assertEqual(self.state(first)["state"], "draining")
        self.apply("checkpoint", first, safe=True, status="saved")
        self.apply("requeue", first)
        self.assertEqual(self.state(next_job)["state"], "running")
        with patch("arbiter.core.time.time", return_value=time.time() + 61):
            self.store.apply("tick", {})
        self.assertEqual(self.state(next_job)["state"], "draining")

    def test_shared_capacity(self):
        a, b, c = self.register(), self.register(), self.register()
        self.assertEqual(self.apply("reserve", a, exclusive=["network"])["state"], "granted")
        self.assertEqual(self.apply("reserve", b, exclusive=["network"])["state"], "granted")
        self.assertEqual(self.apply("reserve", c, exclusive=["network"])["state"], "requested")

    def test_measure_takes_the_whole_machine(self):
        a, m, b = self.register(), self.register("bench"), self.register()
        self.apply("reserve", a, exclusive=["build"])
        rm = self.apply("reserve", m, measure=True)
        self.assertEqual(rm["state"], "requested")
        self.assertIn("Measurement waits", rm["reason"])
        self.apply("finish", a, safe=True)
        self.assertEqual(self.reservation(rm["id"])["state"], "granted")
        rb = self.apply("reserve", b, exclusive=["unrelated"])
        self.assertEqual(rb["state"], "requested")
        self.assertIn("measuring", rb["reason"])

    def test_yielding_job_is_asked_to_step_aside_and_requeues_behind(self):
        y = self.register("trainer", kind="job", yields=True)
        ry = self.apply("reserve", y, exclusive=["gpu"])
        w = self.register("bench")
        rw = self.apply("reserve", w, exclusive=["gpu"])
        self.assertEqual(self.state(y)["state"], "draining")
        pause = [c for c in self.state()["commands"].values() if c["session"] == y][0]
        self.assertIn("bench is waiting for gpu", pause["reason"])
        self.apply("checkpoint", y, safe=True, status="checkpointed")
        self.assertEqual(self.reservation(rw["id"])["state"], "granted")
        self.apply("requeue", y)
        self.assertEqual(self.reservation(ry["id"])["state"], "requested")
        self.apply("finish", w, safe=True)
        self.assertEqual(self.reservation(ry["id"])["state"], "granted")

    def test_ordinary_jobs_are_not_preempted(self):
        a = self.register(kind="job")
        self.apply("reserve", a, exclusive=["gpu"])
        self.apply("reserve", self.register(), exclusive=["gpu"])
        self.assertEqual(self.state(a)["state"], "running")

    def test_disk_claims_count_until_written(self):
        a, b = self.register(kind="hold"), self.register(kind="hold")
        data = self.path.parent / "data"
        first, second = str(data / "a"), str(data / "b")
        ra = self.apply("reserve", a, disk_gb=70, disk_path=first, eta=600)
        self.assertEqual(ra["state"], "granted")
        rb = self.apply("reserve", b, disk_gb=40, disk_path=second, eta=600)
        self.assertEqual(rb["state"], "requested")
        self.assertIn("Disk", rb["reason"])
        self.assertFalse(self.store.disk_check(str(data), 40)["ok"])
        # 60 GB of the claim has been written and free space fell accordingly: 10 GB still owed.
        self.probe.sizes[first], self.probe.free_gb = 60.0, 55.0
        self.store.last_disk = -1e9
        self.store.apply("tick", {})
        self.assertEqual(self.reservation(rb["id"])["state"], "granted")


class AttachTests(Base):
    def test_attach_is_idempotent_and_follows_a_resumed_process(self):
        a = self.attach()
        self.assertEqual(self.attach(), a)
        self.assertEqual(self.attach(pid=300), a)
        self.assertEqual(self.state(a)["pid"], 300)
        self.assertEqual(self.state(a)["name"], "proj-conv")

    def test_conversations_sharing_a_process_stay_separate(self):
        a = self.attach()
        b = self.attach("conv-9")
        self.assertNotEqual(a, b)
        self.assertEqual(self.state(a)["state"], "running")

    def test_attached_session_ends_when_its_agent_exits(self):
        a = self.attach()
        self.probe.pids.discard(100)
        self.store.apply("tick", {})
        self.assertIn(self.state(a)["state"], {"finished", "failed"})

    def test_collect_delivers_requests_and_messages_once(self):
        a, b = self.attach(), self.attach("conv-2", pid=300)
        self.apply("control", a, action="status", reason="how is it going")
        self.apply("message", b, to=a, text="done with the gpu?")
        first = self.store.apply("hook", {"session": a, "collect": True})["inbox"]
        self.assertEqual(len(first["commands"]), 1)
        self.assertEqual(first["messages"][0]["sender"], "proj-conv")
        self.assertIn("arbiter report --command", inbox_text(first))
        second = self.store.apply("hook", {"session": a, "collect": True})["inbox"]
        self.assertEqual(second, {"commands": [], "messages": []})


class GuardTests(Base):
    def guard(self, sid, command):
        return self.store.apply("hook", {"session": sid, "command": command})["guard"]

    def test_matched_resource_held_by_another_conversation_waits(self):
        a, b = self.attach(), self.attach("conv-2", pid=300)
        h = self.register(kind="hold", parent=a)
        self.apply("reserve", h, exclusive=["gpu"], eta=600, label="finetune")
        g = self.guard(b, "llama-server -m model.gguf")
        self.assertEqual(g["decision"], "wait")
        self.assertIn("gpu is held by", g["reason"])
        self.assertIn("finetune", g["reason"])
        self.assertEqual(self.guard(a, "llama-server -m model.gguf")["decision"], "allow")
        self.assertEqual(self.guard(b, "arbiter run gpu -- llama-server -m m")["decision"], "allow")
        self.assertEqual(self.guard(b, "ls -la")["decision"], "allow")

    def test_unwrapped_use_of_a_free_resource_gets_a_nudge(self):
        a = self.attach()
        g = self.guard(a, "llama-server -m m")
        self.assertEqual(g["decision"], "allow")
        self.assertIn("arbiter run --exclusive gpu", g["context"])

    def test_measurement_holds_everyone_else(self):
        a, b = self.attach(), self.attach("conv-2", pid=300)
        m = self.register(kind="job", parent=a)
        self.apply("reserve", m, measure=True)
        self.assertEqual(self.guard(b, "make")["decision"], "wait")
        self.assertEqual(self.guard(a, "make")["decision"], "allow")

    def test_pressure_holds_heavy_commands_unless_the_session_holds_resources(self):
        a = self.attach()
        self.store.apply("tick", {"available_mb": 50})
        self.assertEqual(self.guard(a, "make -j16")["decision"], "wait")
        self.assertEqual(self.guard(a, "git status")["decision"], "allow")
        self.store.apply("tick", {"available_mb": 5000, "cpu_busy": 99})
        self.assertIn("CPU", self.guard(a, "make")["reason"])

    def test_pause_requests_deny_new_work_then_hold_it(self):
        a = self.attach()
        self.apply("control", a, action="pause", reason="let tests run")
        g = self.guard(a, "make")
        self.assertEqual(g["decision"], "deny")
        self.assertIn("arbiter checkpoint", g["reason"])
        self.assertEqual(self.guard(a, "arbiter checkpoint --safe done")["decision"], "allow")
        self.apply("checkpoint", a, safe=True, status="stopped")
        self.assertEqual(self.guard(a, "make")["decision"], "wait")


class FakeClient:
    """Routes hook requests straight into a Store, with operator rights."""
    def __init__(self, store):
        self.store = store

    def action(self, op, **data):
        return self.store.apply(op, data)

    def snapshot(self, wait=None):
        return self.store.snapshot()


class HookProtocolTests(Base):
    def hook(self, event, provider="claude", env=None, **data):
        payload = {"hook_event_name": event, "session_id": data.pop("session_id", "conv-1"), "cwd": "/home/u/proj", **data}
        env = {"CLAUDE_PID": "100"} if env is None else env
        import arbiter.system as system
        original = system.agent_pid
        system.agent_pid = lambda env=None, start=None: 100 if provider == "claude" else 300
        try:
            return Hook(provider, payload, FakeClient(self.store), env=env).run()
        finally:
            system.agent_pid = original

    def test_session_start_attaches_and_explains(self):
        out = self.hook("SessionStart")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("arbiter run gpu", ctx)
        self.assertIn("proj-conv", ctx)

    def test_preferred_phase_reaches_sessions_and_accepts_a_longer_eta(self):
        self.hook("SessionStart")
        before = self.store.board_settings()
        fields = {k: v for k, v in before.items() if k not in {"overridden", "custom_command"}}
        self.store.apply("save_settings", {"expected": before, "settings": {**fields, "preferred_phase_minutes": 60}})
        self.assertNotIn("operator_settings", self.store.state)
        update = self.hook("UserPromptSubmit")
        self.assertIn("60-minute phases", update["hookSpecificOutput"]["additionalContext"])
        started = self.hook("SessionStart", session_id="conv-2", provider="codex")
        self.assertIn("60-minute phases", started["hookSpecificOutput"]["additionalContext"])
        grant = self.apply("reserve", self.register(), exclusive=["gpu"], eta=7200)
        self.assertEqual((grant["state"], grant["duration_s"]), ("granted", 7200))

    def test_blocked_command_is_denied_with_wait_instructions(self):
        self.hook("SessionStart", provider="codex", session_id="other")
        other = next(s["id"] for s in self.state()["sessions"].values() if s.get("provider") == "codex")
        h = self.register(kind="hold", parent=other)
        self.apply("reserve", h, exclusive=["gpu"], eta=600)
        out = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "llama-server -m x"})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(reason.startswith("WAIT, not a refusal"))
        self.assertIn("arbiter wait gpu", reason)

    def test_free_command_passes_silently(self):
        self.assertIsNone(self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}))

    def test_messages_arrive_after_tool_calls_and_block_stopping_once(self):
        self.hook("SessionStart")
        me = next(s["id"] for s in self.state()["sessions"].values() if s.get("external_id") == "conv-1")
        sender = self.register("planner")
        self.apply("message", sender, to=me, text="please re-run the eval")
        out = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(out["decision"], "block")
        self.assertIn("please re-run the eval", out["reason"])
        self.assertIsNone(self.hook("Stop", stop_hook_active=True))
        self.apply("message", sender, to=me, text="second note")
        out = self.hook("PostToolUse", tool_name="Read")
        self.assertIn("second note", out["hookSpecificOutput"]["additionalContext"])

    def test_session_end_releases(self):
        self.hook("SessionStart")
        me = next(s["id"] for s in self.state()["sessions"].values() if s.get("external_id") == "conv-1")
        self.hook("SessionEnd", reason="prompt_input_exit")
        self.assertEqual(self.state(me)["state"], "finished")

    def test_permission_prompt_marks_the_session_until_the_call_settles(self):
        self.hook("SessionStart")
        me = next(s["id"] for s in self.state()["sessions"].values() if s.get("external_id") == "conv-1")
        call = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        self.assertIsNone(self.hook("PermissionRequest", **call))
        note, = self.state(me)["attention"]
        self.assertEqual((note["kind"], note["detail"]), ("permission", "Bash: rm -rf build"))
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"})  # a parallel call
        self.assertTrue(self.state(me).get("attention"))
        self.hook("PostToolUse", **call)
        self.assertNotIn("attention", self.state(me))
        self.hook("PermissionRequest", **call)
        self.hook("Notification", notification_type="elicitation_dialog", message="MCP server requests your input")
        self.assertEqual(len(self.state(me)["attention"]), 2)
        self.hook("Notification", notification_type="idle_prompt", message="waiting")
        self.assertEqual(len(self.state(me)["attention"]), 2)
        self.hook("Stop", stop_hook_active=False)
        self.assertNotIn("attention", self.state(me))

    def test_attention_lapses_and_ends_with_the_session(self):
        self.hook("SessionStart")
        me = next(s["id"] for s in self.state()["sessions"].values() if s.get("external_id") == "conv-1")
        self.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "make"})
        self.store.state["sessions"][me]["attention"][0]["since"] -= 3601
        self.store.apply("tick", {})
        self.assertNotIn("attention", self.state(me))
        self.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "make"})
        self.hook("SessionEnd", reason="other")
        self.assertNotIn("attention", self.state(me))

    def test_managed_children_use_their_session_and_skip_relay(self):
        sid = self.register(kind="claude")
        self.assertIsNone(self.hook("PostToolUse", env={"ARBITER_SESSION": sid}, tool_name="Read"))
        self.assertFalse(any(s.get("provider") for s in self.state()["sessions"].values()))


class PermissionTests(Base):
    def test_session_tokens_register_and_manage_only_their_children(self):
        a, b = self.register(), self.register()
        self.assertIsNone(permitted(a, "register", {"parent": a, "kind": "job"}))
        self.assertIsNotNone(permitted(a, "register", {"parent": b, "kind": "job"}))
        self.assertIsNotNone(permitted(a, "register", {"parent": a, "kind": "claude"}))
        child = self.register(kind="hold", parent=a)
        self.assertIsNone(permitted(a, "finish", {"session": child}, self.store))
        self.assertIsNotNone(permitted(b, "finish", {"session": child}, self.store))
        self.assertIsNotNone(permitted(a, "control", {"session": a}, self.store))
        self.assertIsNotNone(permitted(a, "attach", {"provider": "claude"}, self.store))

    def test_http_child_registration_with_session_token(self):
        from tests.test_http_mcp import LocalHTTPClient
        server = SimpleNamespace(store=self.store, token="t" * 40, server_port=7483, unix=False, telemetry={},
                                 stopping=threading.Event(), launcher=Launcher())
        parent = self.store.apply("register", {"name": "agent"})
        code, _, raw = LocalHTTPClient(server).exchange("/api/action", {"action": "register", "data": {
            "name": "job", "kind": "job", "parent": parent["id"]}}, token=parent["token"])
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["parent"], parent["id"])


class PureTests(unittest.TestCase):
    def test_read_only_classification(self):
        self.assertTrue(guard.read_only("ls -la && git status | head"))
        self.assertTrue(guard.read_only("FOO=1 timeout 5 grep -r x ."))
        self.assertFalse(guard.read_only("make -j8"))
        self.assertFalse(guard.read_only("ls; python train.py"))
        self.assertTrue(guard.wrapped("arbiter run gpu -- python bench.py"))
        self.assertTrue(guard.wrapped("cd x && arbiter run gpu -- llama-server"))
        self.assertFalse(guard.wrapped("python bench.py"))

    def test_durations(self):
        self.assertEqual(duration("90s"), 90)
        self.assertEqual(duration("40m"), 2400)
        self.assertEqual(duration("1h30m"), 5400)
        self.assertEqual(duration("15"), 900)
        with self.assertRaises(ValueError):
            duration("soon")


class InstallTests(unittest.TestCase):
    def test_hooks_merge_idempotently_and_uninstall_cleanly(self):
        from arbiter import install
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"model": "x", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "codegraph prompt-hook"}]}]}}))
            install.set_hooks(path, "claude")
            install.set_hooks(path, "claude")
            data = json.loads(path.read_text())
            self.assertEqual(data["model"], "x")
            ups = data["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(ups), 2)
            self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "^(Bash|PowerShell)$" if os.name == "nt" else "Bash")
            self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], 300)
            install.set_hooks(path, None)
            data = json.loads(path.read_text())
            self.assertEqual(data["hooks"], {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "codegraph prompt-hook"}]}]})

    def test_claude_child_routes_permission_prompts_and_allows_routine_tools(self):
        from arbiter.adapters import claude_command
        argv = claude_command("/tmp/x.json")
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn("mcp__arbiter__arbiter_report", argv[argv.index("--allowedTools") + 1])
        self.assertNotIn("arbiter_finish", argv[argv.index("--allowedTools") + 1])


if __name__ == "__main__":
    unittest.main()
