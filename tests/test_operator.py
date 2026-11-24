import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbiter.config import Config
from arbiter.core import Store
from arbiter.operator import ask_model, candidates, parse_decision, run


class OperatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.db", config=Config(data={"operator": {
            "provider": "command", "command": [sys.executable], "approve_inbox": True, "manage_resources": True,
        }}))
        self.addCleanup(self.store.db.close)
        self.policy = self.store.config["operator"]

    def create_session(self, name):
        return self.store.apply("register", {"name": name})["id"]

    def choose(self, choice, choices):
        return self.store.apply("operator_decision", {
            "choice": choice, "candidate": choices[choice], "policy": self.policy, "reason": "Routine coordination",
        })

    def test_only_inbox_confirmation_is_eligible_and_stale_decision_is_rejected(self):
        session = self.create_session("session")
        inbox = self.store.apply("approval_request", {
            "session": session, "method": "claude/can_use_tool",
            "params": {"tool_name": "mcp__arbiter__arbiter_inbox", "input": {}},
        })["id"]
        command = self.store.apply("approval_request", {
            "session": session, "method": "item/commandExecution/requestApproval",
            "params": {"command": "arbiter status"},
        })["id"]
        choices = candidates(self.store.state, self.policy, time.time())
        self.assertEqual(list(choices), [inbox])
        decision = self.choose(inbox, choices)
        state = self.store.snapshot()
        self.assertEqual(state["approvals"][inbox]["response"], {"behavior": "allow", "updatedInput": {}})
        self.assertEqual(state["approvals"][command]["state"], "pending")
        self.assertEqual(state["operator_decisions"], [decision])
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.choose(inbox, choices)

    def test_priority_boost_does_not_override_a_held_lock(self):
        owner = self.create_session("owner")
        waiter = self.create_session("waiter")
        self.store.apply("reserve", {"session": owner, "exclusive": ["gpu"]})
        reservation = self.store.apply("reserve", {"session": waiter, "exclusive": ["gpu"]})["id"]
        self.store.state["reservations"][reservation]["created"] -= 40
        choices = candidates(self.store.state, self.policy, time.time())
        self.assertEqual(choices[owner]["action"], "request_update")
        self.choose(reservation, choices)
        state = self.store.snapshot()
        self.assertEqual(state["reservations"][reservation]["state"], "requested")
        self.assertEqual(state["reservations"][reservation]["priority"], 10)
        self.assertNotIn(reservation, candidates(self.store.state, self.policy, time.time()))

    def test_custom_model_runner_and_strict_output(self):
        script = ("import os, sys, time;"
                  "assert not any(k.startswith('ARBITER_') for k in os.environ);"
                  "time.sleep(1.1); prompt=sys.stdin.read(); assert 'candidate' in prompt and 'Favor builds' in prompt;"
                  "print('{\"choice\":\"candidate\",\"reason\":\"Looks routine\"}')")
        policy = dict(self.policy, instructions="Favor builds", command=[sys.executable, "-c", script])
        choices = {"candidate": {"action": "request_update"}}
        self.assertEqual(ask_model(policy, choices, threading.Event()), ("candidate", "Looks routine"))
        with self.assertRaisesRegex(ValueError, "unknown candidate"):
            parse_decision(json.dumps({"choice": "other", "reason": "Unexpected"}), choices)

    def test_model_free_settings_and_preset_library(self):
        before = self.store.board_settings()
        fields = {k: v for k, v in before.items() if k not in {"custom_command", "overridden"}}
        model = {k: self.policy[k] for k in ("provider", "model", "approve_inbox", "manage_resources",
                                             "reply_messages", "review_status", "instructions")}
        model["instructions"] = "Prefer interactive builds."
        presets = [{"name": "Interactive", "settings": model}]
        fields.update(provider="", auto_approve_inbox=True, preserve_interactivity=True, presets=presets)
        saved = self.store.apply("save_settings", {"expected": before, "settings": fields})
        sid = self.create_session("worker")
        def approval(tool):
            return self.store.apply("approval_request", {"session": sid, "method": "claude/can_use_tool",
                                    "params": {"tool_name": tool, "input": {}}})["state"]
        self.assertEqual(approval("mcp__arbiter__arbiter_inbox"), "answered")
        self.assertEqual(approval("Bash"), "pending")
        self.store.apply("tick", {"gpu_pressure": ["card1 GPU 96% busy"]})
        rid = self.store.apply("reserve", {"session": sid, "exclusive": ["gpu"]})["id"]
        self.assertEqual(self.store.state["reservations"][rid]["state"], "requested")
        self.store.apply("tick", {"gpu_pressure": [], "cpu_busy": 99})
        self.assertIn("CPU", self.store.state["reservations"][rid]["reason"])
        self.store.apply("tick", {"cpu_busy": 10})
        self.assertEqual(self.store.state["reservations"][rid]["state"], "granted")
        with self.assertRaises(ValueError):
            self.store.apply("save_settings", {"expected": saved, "settings": {**fields, "presets": presets * 2}})
        self.assertEqual(saved, self.store.board_settings())
        reset = self.store.apply("save_settings", {"expected": saved, "settings": None})
        self.assertFalse(reset["auto_approve_inbox"])
        self.assertEqual(reset["presets"], presets)

    def test_incoming_messages_trigger_threaded_replies_without_an_echo(self):
        sender, peer = self.create_session("sender"), self.create_session("peer")
        def send(text, **extra):
            return self.store.apply("message", {"session": sender, "text": text, **extra})["id"]
        send("Before enabling", to="arbiter")
        self.policy["reply_messages"] = True
        send("Private", to=peer)
        direct = send("Is it my turn?", to="arbiter")
        broadcast = send("I can yield the GPU")
        def respond(policy, choices, stopping):
            if direct in choices:
                self.assertEqual(set(choices), {direct, broadcast})
                self.assertEqual(choices[direct]["text"], "Is it my turn?")
                return direct, "Wait for a grant."
            self.assertEqual(list(choices), [broadcast])
            return None, "Informational"
        stop = threading.Event()
        with patch("arbiter.operator.ask_model", side_effect=respond) as model, patch.object(self.store.changed, "wait", side_effect=stop.set):
            run(self.store, stop)
        self.assertEqual(model.call_count, 2)
        reply, = self.store.inbox(sender)["messages"]
        self.assertEqual((reply["from"], reply["reply_to"], reply["text"]), ("arbiter", direct, "Wait for a grant."))
        send("Thanks", to="arbiter", reply_to=reply["id"])
        self.assertEqual(self.store.state["operator_events"], {})

    def test_changed_status_is_coalesced_and_revalidated(self):
        self.policy["review_status"] = True
        sid = self.create_session("worker")
        key = "status/" + sid
        report = {"session": sid, "status": "Compiling", "blocker": "Missing dependency"}
        self.store.apply("report", report)
        previous = candidates(self.store.state, self.policy, time.time())
        self.assertEqual(previous[key]["blocker"], "Missing dependency")
        self.store.apply("report", {**report, "blocker": "Waiting on a download"})
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.choose(key, previous)
        self.store.apply("operator_events_reviewed", {"policy": self.policy, "events": previous})
        current = candidates(self.store.state, self.policy, time.time())
        self.assertEqual(self.choose(key, current)["action"], "flag_status")
        self.store.apply("report", {**report, "blocker": "Waiting on a download"})
        self.store.apply("heartbeat", {"session": sid})
        self.assertEqual(candidates(self.store.state, self.policy, time.time()), {})
        self.store.apply("finish", {"session": sid, "safe": True})
        self.assertEqual(self.store.state["operator_events"][key]["state"], "finished")

    def test_message_can_reopen_a_queue_decision_for_its_sender(self):
        self.policy["reply_messages"] = True
        holder, waiter = self.create_session("holder"), self.create_session("waiter")
        self.store.apply("reserve", {"session": holder, "exclusive": ["gpu"]})
        rid = self.store.apply("reserve", {"session": waiter, "exclusive": ["gpu"]})["id"]
        self.store.state["reservations"][rid]["created"] -= 40
        stop = threading.Event()
        def ask(policy, choices, stopping):
            if rid in choices and any(c["action"] == "reply_message" for c in choices.values()):
                return rid, "Prioritize this waiting reservation."
            return None, "No action"
        def send():
            self.store.apply("message", {"session": waiter, "to": "arbiter", "text": "Please prioritize this job."})
        wakeups = iter([send, stop.set])
        with patch("arbiter.operator.ask_model", side_effect=ask), patch.object(self.store.changed, "wait", side_effect=lambda: next(wakeups)()):
            run(self.store, stop)
        self.assertTrue(self.store.state["reservations"][rid]["operator_promoted"])
        self.assertEqual(self.store.state["reservations"][rid]["state"], "requested")

    def test_model_runs_only_for_new_demand_even_after_a_decline_or_error(self):
        session = self.create_session("worker")
        stop = threading.Event()
        requests = []
        def new_inbox():
            request = self.store.apply("approval_request", {"session": session, "method": "claude/can_use_tool",
                                       "params": {"tool_name": "mcp__arbiter__arbiter_inbox", "input": {}}})
            requests.append(request["id"])

        def answer(policy, choices, stopping):
            self.assertEqual(list(choices), [requests[-1]])
            if len(requests) == 2:
                raise ValueError("offline")
            return None, "Leave for operator"

        # Simulate store wakeups over hours: idle, new request, unchanged work,
        # another request, and more ticks after a provider failure.
        wakeups = iter([lambda: None, new_inbox, lambda: None, new_inbox, lambda: None, stop.set])
        with patch.object(self.store.changed, "wait", side_effect=lambda: next(wakeups)()), \
             patch("arbiter.operator.time.time", side_effect=range(100000, 1000000, 600)), \
             patch("arbiter.operator.ask_model", side_effect=answer) as model, patch("sys.stderr"):
            run(self.store, stop)
        self.assertEqual(model.call_count, 2)
