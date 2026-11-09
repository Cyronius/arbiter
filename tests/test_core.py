import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path

from arbiter.core import Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.store = Store(self.path, cpu=2, memory_mb=1024, headroom_mb=100)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def session(self, name="worker", **extra):
        return self.store.apply("register", {"name": name, **extra})["id"]

    def apply(self, operation, sid, **extra):
        return self.store.apply(operation, {"session": sid, **extra})

    def test_complete_coordination_loop(self):
        a, b = self.session("codex", kind="codex"), self.session("claude", kind="claude")
        ra = self.apply("reserve", a, cpu=1, exclusive=["repo"])
        rb = self.apply("reserve", b, cpu=1, exclusive=["repo"])
        self.assertEqual(rb["state"], "requested")
        command = self.apply("control", a, action="status")
        self.apply("report", a, status="finishing batch", command=command["id"])
        message = self.apply("message", b, to=a, text="Please yield", topic="handoff")
        self.assertEqual(self.store.inbox(a)["messages"][0]["id"], message["id"])
        self.apply("ack_message", a, message=message["id"])
        self.assertFalse(self.store.inbox(a)["messages"])
        self.apply("control", a, action="pause")
        self.assertEqual(self.store.snapshot()["reservations"][ra["id"]]["state"], "granted")
        with self.assertRaises(ValueError):
            self.apply("checkpoint", a, safe=False, status="not safe")
        self.apply("checkpoint", a, safe=True, status="children stopped")
        self.assertEqual(self.store.snapshot()["reservations"][rb["id"]]["state"], "granted")
        self.apply("control", a, action="resume")
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "waiting")
        self.apply("finish", b, safe=True)
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")
        self.assertEqual(self.store.snapshot()["reservations"][ra["id"]]["state"], "granted")

    def test_cpu_and_memory_share_but_do_not_exceed_budget(self):
        a, b, c = (self.session() for _ in range(3))
        self.assertEqual(self.apply("reserve", a, cpu=.75, memory_mb=400)["state"], "granted")
        self.assertEqual(self.apply("reserve", b, cpu=.75, memory_mb=400)["state"], "granted")
        self.assertEqual(self.apply("reserve", c, cpu=.75, memory_mb=400)["state"], "requested")
        self.apply("finish", a, safe=True)
        self.assertEqual(self.store.snapshot()["sessions"][c]["state"], "running")

    def test_concurrent_exclusive_requests_have_one_winner(self):
        sessions = [self.session(str(i)) for i in range(16)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda sid: self.apply("reserve", sid, exclusive=["gpu"]), sessions))
        self.assertEqual(sum(r["state"] == "granted" for r in results), 1)

    def test_heartbeat_expiry_does_not_release_ownership(self):
        a, b = self.session(), self.session()
        ra = self.apply("reserve", a, exclusive=["device"])
        rb = self.apply("reserve", b, exclusive=["device"])
        self.store.state["sessions"][a]["heartbeat"] = time.time() - 100
        self.store.apply("tick", {})
        state = self.store.snapshot()
        self.assertEqual(state["sessions"][a]["state"], "stale")
        self.assertEqual(state["reservations"][ra["id"]]["state"], "granted")
        self.assertEqual(state["reservations"][rb["id"]]["state"], "requested")
        self.apply("heartbeat", a)
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_restart_retains_reservations_and_events(self):
        a = self.session()
        r = self.apply("reserve", a, exclusive=["device"])
        old_cursor = self.store.snapshot()["cursor"]
        self.store.db.close()
        self.store = Store(self.path, cpu=2, memory_mb=1024)
        state = self.store.snapshot()
        self.assertEqual(state["sessions"][a]["state"], "stale")
        self.assertEqual(state["reservations"][r["id"]]["state"], "granted")
        self.assertGreater(state["cursor"], old_cursor)

    def test_invalid_report_rolls_back_state_and_events(self):
        a = self.session()
        before = self.store.snapshot()
        with self.assertRaises(ValueError):
            self.apply("report", a, status="should roll back", command="does-not-exist")
        self.assertEqual(before, self.store.snapshot())

    def test_no_hold_and_wait(self):
        a = self.session()
        self.apply("reserve", a, cpu=1)
        with self.assertRaisesRegex(ValueError, "release existing"):
            self.apply("reserve", a, exclusive=["repo"])

    def test_stale_queued_worker_is_not_granted_resources(self):
        a, b = self.session(), self.session()
        self.apply("reserve", a, cpu=2)
        self.apply("reserve", b, cpu=1)
        self.store.state["sessions"][b]["heartbeat"] = time.time() - 100
        self.store.apply("tick", {})
        self.apply("finish", a, safe=True)
        self.assertEqual(self.store.snapshot()["sessions"][b]["state"], "stale")
        self.apply("heartbeat", b)
        self.assertEqual(self.store.snapshot()["sessions"][b]["state"], "running")

    def test_unanswered_status_request_remains_visible(self):
        a = self.session()
        c = self.apply("control", a, action="status")
        self.store.state["commands"][c["id"]]["deadline"] = time.time() - 1
        self.store.apply("tick", {})
        self.assertEqual(self.store.inbox(a)["commands"][0]["state"], "unanswered")
        self.apply("report", a, status="late reply", command=c["id"])
        self.assertFalse(self.store.inbox(a)["commands"])

    def test_pause_prevents_new_work(self):
        a = self.session()
        self.apply("control", a, action="pause")
        with self.assertRaises(ValueError):
            self.apply("reserve", a, cpu=1)

    def test_resume_cancels_unacknowledged_pause(self):
        a = self.session()
        c = self.apply("control", a, action="pause")
        self.apply("control", a, action="resume")
        self.assertEqual(self.store.snapshot()["commands"][c["id"]]["state"], "cancelled")
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_memory_pressure_pause_and_recovery(self):
        a = self.session(kind="codex", preemptible=True)
        r = self.apply("reserve", a, memory_mb=400)
        self.store.apply("tick", {"available_mb": 50})
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "draining")
        self.assertEqual(self.store.snapshot()["reservations"][r["id"]]["state"], "granted")
        self.apply("checkpoint", a, safe=True, status="job memory released")
        self.store.recovered_since = time.time() - 16
        self.store.apply("tick", {"available_mb": 900})
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_memory_pressure_does_not_preempt_ordinary_jobs(self):
        a = self.session(kind="job")
        self.apply("reserve", a, memory_mb=400)
        self.store.apply("tick", {"available_mb": 50})
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_memory_admission_uses_measured_headroom(self):
        a = self.session()
        self.store.apply("tick", {"available_mb": 250})
        r = self.apply("reserve", a, memory_mb=200)
        self.assertEqual(r["state"], "requested")
        self.assertIn("headroom", r["reason"])
        self.store.apply("tick", {"available_mb": 600})
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_backfill_and_aged_head_protection(self):
        a, b, c = self.session(), self.session(), self.session()
        self.apply("reserve", a, cpu=1)
        waiting = self.apply("reserve", b, cpu=2)
        self.assertEqual(self.apply("reserve", c, exclusive=["unrelated"])["state"], "granted")
        d = self.session()
        self.store.state["reservations"][waiting["id"]]["created"] = time.time()-35
        self.assertEqual(self.apply("reserve", d, cpu=.5)["state"], "requested")

    def test_invalid_quantities(self):
        a = self.session()
        for cpu in [float('nan'), float('inf'), -1, True, "2", 3]:
            with self.subTest(cpu=cpu), self.assertRaises(ValueError):
                self.apply("reserve", a, cpu=cpu)

    def test_finish_requires_safe_confirmation(self):
        a = self.session()
        with self.assertRaises(ValueError):
            self.apply("finish", a)

    def test_message_reply_and_recipient_checks(self):
        a,b,c = self.session(),self.session(),self.session()
        m = self.apply("message", a, to=b, text="hello")
        with self.assertRaises(ValueError):
            self.apply("ack_message", c, message=m["id"])
        reply=self.apply("message", b, to=a, text="hi", reply_to=m["id"])
        self.assertEqual(reply["reply_to"], m["id"])

    def test_snapshot_does_not_leak_session_tokens(self):
        a=self.session()
        self.assertNotIn("token", self.store.snapshot()["sessions"][a])

    def test_approval_response_is_durable(self):
        a=self.session()
        req=self.apply("approval_request", a, method="item/fileChange/requestApproval", params={"reason":"edit"})
        self.apply("approval_answer",a,approval=req["id"],response={"decision":"decline"})
        self.assertEqual(self.store.snapshot()["approvals"][req["id"]]["response"],{"decision":"decline"})
