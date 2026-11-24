"""Durable recovery decisions and process identity across restart."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from arbiter import system
from arbiter.core import Store
from arbiter.launch import tracked_popen
from tests.test_features import FakeProbe


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "state.db"
        self.probe = FakeProbe()
        self.store = Store(self.path, probe=self.probe)
        self.addCleanup(lambda: self.store.db.close())

    def restart(self):
        self.store.db.close()
        self.store = Store(self.path, probe=self.probe)

    def job(self, launch=None):
        record = {"argv": ["fixture", "", "private argument"], "bundle": {"exclusive": ["fixture"]}}
        return self.store.apply("register", dict(name="shards", kind="job", cwd=str(self.directory),
                                                  launch=launch or record, auto_retry=True, pid=100))["id"]

    def act(self, operation, sid, **extra):
        return self.store.apply(operation, dict(session=sid, **extra))

    def test_transactional_claim_and_private_record(self):
        sid = self.job()
        self.act("finish", sid, safe=True, failed=True, returncode=1)
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.act("recover", sid)["id"], range(12)))
        child, = set(results)
        self.restart()
        self.assertEqual(self.act("recover", sid)["id"], child)
        self.assertEqual(self.store.session(child)["original"], sid)
        self.assertNotEqual(self.store.session(child)["token"], self.store.session(sid)["token"])
        self.assertEqual(len(self.store.state["reservations"]), 1)
        self.assertEqual(self.store.launch_record(self.store.session(child))["argv"][1:], ["", "private argument"])
        self.assertNotIn("private argument", json.dumps(self.store.snapshot()) + json.dumps(self.store.events()))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_empty_bundle_recovery_obeys_interactivity_and_measurement(self):
        sid = self.job(launch={"argv": ["true"], "bundle": {}})
        self.act("finish", sid, safe=True, failed=True, returncode=1)
        holder = self.store.apply("register", {"name": "measurement"})["id"]
        self.act("reserve", holder, measure=True)
        self.store.state["scheduler_settings"] = {"preserve_interactivity": True}
        self.store.apply("tick", {"available_mb": 0})
        child = self.act("recover", sid)["id"]
        rid = self.store.session(child)["recovery_reservation"]
        record = self.store.state["reservations"][rid]
        self.assertEqual(record["state"], "requested")
        self.assertIn("headroom", record["reason"])
        self.store.apply("tick", {"available_mb": 2000})
        self.assertIn("measuring", record["reason"])
        self.act("finish", holder, safe=True)
        self.assertEqual(record["state"], "granted")

    def test_higher_priority_yielder_hands_off_before_rejoining_queue(self):
        sid = self.store.apply("register", {"name": "yielding", "kind": "job", "yields": True})["id"]
        held = self.act("reserve", sid, exclusive=["fixture"], priority=25)
        peer = self.store.apply("register", {"name": "peer"})["id"]
        waiting = self.act("reserve", peer, exclusive=["fixture"])
        self.act("handoff", sid, reservation=held["id"])
        self.assertEqual(self.store.state["reservations"][waiting["id"]]["state"], "granted")
        self.assertEqual(self.store.state["reservations"][held["id"]]["state"], "requested")
        self.assertEqual(self.store.session(sid)["yield_count"], 1)

    def test_missing_directory_and_missing_guid_explanations(self):
        sid = self.job()
        self.act("finish", sid, safe=True)
        self.store.session(sid)["cwd"] = str(self.directory / "absent")
        with self.assertRaisesRegex(ValueError, "working directory"):
            self.act("recover", sid)
        sid = self.store.apply("register", {"name": "managed", "kind": "claude", "cwd": str(self.directory),
                                           "capabilities": ["resume-session"]})["id"]
        self.act("finish", sid, safe=True)
        with self.assertRaisesRegex(ValueError, "GUID"):
            self.act("recover", sid)

    def test_crash_before_dispatch_ack_reuses_claim_and_after_track_requires_manual(self):
        source = self.job()
        self.act("finish", source, safe=True, failed=True, returncode=9)
        sid = self.act("recover", source)["id"]
        self.act("dispatch", sid, pid=200)
        self.probe.pids.remove(200)
        self.restart()
        self.assertNotIn("pid", self.store.session(sid))
        self.assertTrue(self.act("dispatch", sid, pid=300))
        self.assertIsNone(self.act("dispatch", sid, pid=300))
        self.act("track", sid, pgid=555)
        self.probe.groups.add(555)
        self.probe.pids.remove(300)
        self.restart()
        self.assertEqual(self.store.session(sid)["state"], "stale")
        with self.assertRaisesRegex(ValueError, "still run"):
            self.act("recover", sid)
        with self.assertRaisesRegex(ValueError, "still running"):
            self.act("finish", sid, safe=True)
        self.probe.groups.clear()
        self.restart()
        self.assertNotIn("retry_at", self.store.session(sid))
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.act("recover", sid)
        self.assertIn("id", self.act("recover", sid, confirm_unknown=True))

    def test_expiry_revalidates_capacity_and_preserves_lineage_on_failure(self):
        sid = self.job()
        rec = self.store.launch_record(self.store.session(sid))
        rec["bundle"]["cpu"] = 3
        with self.store.db:
            self.store.db.execute("UPDATE launches SET record=?", (json.dumps(rec),))
        self.act("finish", sid, safe=True, failed=True, returncode=1)
        self.store.capacity["cpu"] = 1
        with patch("time.time", return_value=self.store.session(sid)["retry_at"]):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                self.act("auto_recover", sid)
        self.assertEqual(list(self.store.state["sessions"]), [sid])

    def test_retry_budget_restores_deadlines_and_never_resets_on_manual_claim(self):
        root = sid = self.job()
        for used, delay in enumerate([30, 120]):
            self.act("finish", sid, safe=True, failed=True, returncode=3)
            due = self.store.session(sid)["retry_at"]
            self.assertEqual(due - self.store.session(sid)["ended"], delay)
            self.restart()
            with patch("time.time", return_value=due - .01):
                self.assertIsNone(self.act("auto_recover", sid))
            with patch("time.time", return_value=due):
                sid = self.act("auto_recover", sid)["id"]
            self.assertEqual(self.store.session(sid)["retries_used"], used + 1)
            self.assertEqual(self.store.session(sid)["original"], root)
        self.act("finish", sid, safe=True, failed=True, returncode=3)
        self.assertNotIn("retry_at", self.store.session(sid))
        manual = self.act("recover", sid)["id"]
        self.assertEqual(self.store.session(manual)["retries_used"], 2)

    def test_cancellation_oom_and_unknown_outcomes(self):
        for fields, outcome in [({"oom": True, "returncode": -9}, "oom"), ({"cancelled": True}, "cancelled"),
                                ({"uncertain": True}, "unknown"), ({"yielded": True, "returncode": 75}, "yielded")]:
            sid = self.job()
            self.act("finish", sid, safe=True, failed=True, **fields)
            self.assertEqual(self.store.session(sid)["outcome"], outcome)
            self.assertNotIn("retry_at", self.store.session(sid))
        sid = self.job()
        self.act("finish", sid, safe=True, failed=True, returncode=-9)
        self.assertEqual(self.store.session(sid)["outcome"], "failure")
        self.assertEqual(self.store.session(sid)["signal"], 9)
        due = self.store.session(sid)["retry_at"]
        self.act("cancel_retry", sid)
        self.restart()
        with patch("time.time", return_value=due):
            self.assertIsNone(self.act("auto_recover", sid))
        self.assertTrue(self.store.session(sid)["retry_cancelled"])

    def test_pid_reuse_and_boot_change(self):
        for reboot in (False, True):
            sid = self.job()
            self.act("track", sid, pgid=200)
            self.probe.groups.add(200)
            self.probe.pids.remove(100)
            if reboot:
                self.store.session(sid)["boot_id"] = "another boot"
            with patch.object(self.probe, "start_time", return_value=999 if not reboot else 1):
                self.store.apply("tick", {})
            self.assertEqual(self.store.session(sid)["state"], "failed")
            self.probe.pids.add(100)

    def test_exec_waits_for_ack_and_exits_on_owner_death(self):
        marker = self.directory / "executed"
        ready = self.directory / "gate-pid"
        code = """
import os, sys, time
from pathlib import Path
from arbiter.launch import tracked_popen
def wait(pid):
    Path(sys.argv[2]).write_text(str(pid))
    time.sleep(30)
tracked_popen(['touch', sys.argv[1]], wait)
"""
        with subprocess.Popen([sys.executable, "-c", code, str(marker), str(ready)]) as supervisor:
            try:
                for _ in range(200):
                    if ready.exists() and ready.stat().st_size:
                        break
                    time.sleep(.01)
                child = int(ready.read_text())
            finally:
                supervisor.kill()
        for _ in range(200):
            if not system.alive(child):
                break
            time.sleep(.01)
        self.assertFalse(system.alive(child))
        self.assertFalse(marker.exists())
        self.assertEqual(tracked_popen(["touch", str(marker)], lambda _: None).wait(), 0)
        self.assertTrue(marker.exists())
