import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbiter.core import SCHEMA_VERSION, STALE_CHECK_COOLDOWN_S, STALE_CHECK_TIMEOUT_S, Store


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

    def test_promoted_bundle_gets_the_next_conflicting_slot(self):
        owner = self.session()
        self.apply("reserve", owner, exclusive=["gpu"])
        bundle = self.apply("reserve", self.session(), exclusive=["gpu", "npu"])
        self.store.apply("promote", {"reservation": bundle["id"]})
        competing = self.apply("reserve", self.session(), exclusive=["npu"], priority=100)
        self.assertEqual(competing["state"], "requested")
        self.assertEqual(self.apply("reserve", self.session(), exclusive=["disk-io"])["state"], "granted")
        self.apply("finish", owner, safe=True)
        reservations = self.store.snapshot()["reservations"]
        self.assertEqual(reservations[bundle["id"]]["state"], "granted")
        self.assertNotIn("promotion_order", reservations[bundle["id"]])
        self.assertEqual(reservations[competing["id"]]["state"], "requested")

    def test_concurrent_exclusive_requests_have_one_winner(self):
        sessions = [self.session(str(i)) for i in range(16)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda sid: self.apply("reserve", sid, exclusive=["gpu"]), sessions))
        self.assertEqual(sum(r["state"] == "granted" for r in results), 1)

    def test_heartbeat_expiry_does_not_release_ownership(self):
        a, b = self.session(), self.session()
        ra = self.apply("reserve", a, exclusive=["device"])
        rb = self.apply("reserve", b, exclusive=["device"])
        self.store.beats[a] -= 100
        self.store.apply("tick", {})
        state = self.store.snapshot()
        self.assertEqual(state["sessions"][a]["state"], "stale")
        self.assertEqual(state["reservations"][ra["id"]]["state"], "granted")
        self.assertEqual(state["reservations"][rb["id"]]["state"], "requested")
        self.apply("heartbeat", a)
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")

    def test_stale_blocker_check_is_deliverable_and_retries_after_cooldown(self):
        holder, waiter, other = self.session("holder"), self.session("waiter"), self.session("other")
        held = self.apply("reserve", holder, exclusive=["gpu"])
        self.store.beats[holder] -= 100
        self.store.apply("tick", {})
        self.assertFalse(self.store.inbox(holder)["commands"])
        queued = self.apply("reserve", waiter, exclusive=["gpu"])
        self.apply("reserve", other, exclusive=["gpu"])
        first, = self.store.inbox(holder)["commands"]
        self.assertIn("waiter is waiting", first["reason"])

        late = first["created"] + 2 * STALE_CHECK_COOLDOWN_S
        with patch("arbiter.core.time.time", return_value=late):
            self.store.apply("tick", {})
            state = self.store.snapshot()
            self.assertEqual(state["sessions"][holder]["stale_check"]["unanswered_since"], first["deadline"])
            self.assertEqual(state["reservations"][held["id"]]["state"], "granted")
            self.assertEqual(state["reservations"][queued["id"]]["state"], "requested")
            self.assertEqual(self.store.inbox(holder)["commands"], [first])
            delivered, = self.apply("hook", holder, collect=True)["inbox"]["commands"]
            self.assertEqual(delivered["id"], first["id"])
            self.assertEqual(delivered["deadline"], late + STALE_CHECK_TIMEOUT_S)

        with patch("arbiter.core.time.time", return_value=late + STALE_CHECK_COOLDOWN_S - 1):
            self.store.apply("tick", {})
            unanswered, = self.store.inbox(holder)["commands"]
            self.assertEqual(unanswered["id"], first["id"])
            self.assertEqual(unanswered["state"], "unanswered")

        with patch("arbiter.core.time.time", return_value=late + STALE_CHECK_COOLDOWN_S):
            self.store.apply("tick", {})
            retry, = self.store.inbox(holder)["commands"]
            self.assertNotEqual(retry["id"], first["id"])
            self.apply("report", holder, status="Still using the GPU", command=retry["id"])
        state = self.store.snapshot()
        self.assertEqual(state["sessions"][holder]["state"], "running")
        self.assertNotIn("unanswered_since", state["sessions"][holder]["stale_check"])
        self.assertEqual(state["reservations"][held["id"]]["state"], "granted")
        self.assertFalse(self.store.inbox(holder)["commands"])
        self.assertEqual(sum(event["kind"] == "session.stale_check_unanswered" for event in self.store.events()), 1)

    def test_stale_blocker_alert_clears_after_waiter_leaves(self):
        holder, waiter = self.session(), self.session()
        held = self.apply("reserve", holder, cpu=2)
        self.apply("reserve", waiter, cpu=1)
        self.store.beats[holder] -= 100
        self.store.apply("tick", {})
        first, = self.store.inbox(holder)["commands"]
        with patch("arbiter.core.time.time", return_value=first["deadline"] + 1):
            self.store.apply("tick", {})
            self.assertIn("unanswered_since", self.store.snapshot()["sessions"][holder]["stale_check"])
            self.apply("finish", waiter, safe=True)
        state = self.store.snapshot()
        self.assertEqual(state["reservations"][held["id"]]["state"], "granted")
        self.assertNotIn("unanswered_since", state["sessions"][holder]["stale_check"])
        self.assertFalse(self.store.inbox(holder)["commands"])

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
        self.store.beats[b] -= 100
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

    def test_heartbeat_is_not_persisted_but_revives_stale_sessions(self):
        a = self.session()
        cursor = self.store.cursor()
        before = self.db_state()
        self.apply("heartbeat", a)
        self.assertEqual(self.store.cursor(), cursor)
        self.assertEqual(self.db_state(), before)
        self.store.beats[a] -= 100
        self.store.apply("tick", {})
        self.apply("heartbeat", a)
        self.assertEqual(self.store.snapshot()["sessions"][a]["state"], "running")
        self.assertGreater(self.store.cursor(), cursor)

    def db_state(self):
        return self.store.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0]

    def test_failed_action_restores_pressure_tracking(self):
        self.store.available_mb, self.store.recovered_since = 500, 123.0
        with self.assertRaises(ValueError):
            self.store.apply("reserve", {"session": "nope"})
        self.assertEqual((self.store.available_mb, self.store.recovered_since), (500, 123.0))

    def test_prune_forgets_old_finished_sessions_and_keeps_cursor(self):
        self.store.retention_days = 1
        old, new = self.session("old"), self.session("new")
        self.apply("reserve", old, cpu=1)
        self.apply("message", old, text="hello")
        self.apply("finish", old, safe=True)
        self.apply("finish", new, safe=True)
        self.store.state["sessions"][old]["ended"] = time.time() - 3 * 86400
        cursor = self.store.cursor()
        self.store.last_prune = -1e9
        self.store.apply("tick", {})
        state = self.store.snapshot()
        self.assertNotIn(old, state["sessions"])
        self.assertIn(new, state["sessions"])
        self.assertEqual(state["reservations"], {})
        self.assertEqual(state["messages"], {})
        self.assertGreaterEqual(self.store.cursor(), cursor)
        self.assertTrue(any(e["kind"] == "state.pruned" for e in self.store.events()))

    def test_schema_version_is_recorded_and_newer_state_is_refused(self):
        self.assertEqual(self.store.state["schema_version"], SCHEMA_VERSION)
        self.store.state["schema_version"] = 99
        self.store.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.store.state),))
        self.store.db.commit()
        with self.assertRaisesRegex(RuntimeError, "newer Arbiter"):
            Store(self.path)

    def test_session_token_lookup(self):
        a = self.session()
        token = self.store.state["sessions"][a]["token"]
        self.assertEqual(self.store.session_for_token(token), a)
        self.assertIsNone(self.store.session_for_token("wrong"))
        self.assertIsNone(self.store.session_for_token("é"))
        self.apply("finish", a, safe=True)
        self.assertIsNone(self.store.session_for_token(token))
