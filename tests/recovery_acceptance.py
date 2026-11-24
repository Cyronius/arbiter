"""Opt-in, Linux end-to-end check: python -m tests.recovery_acceptance.

Uses a temporary TCP daemon, database, provider executables and Chromium profile.
Requires chromium and chromedriver. Includes the full 30s and 120s retry delays.
"""
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from arbiter.client import Client
from arbiter import system

ROOT = Path(__file__).resolve().parents[1]
ENTRY = str(ROOT / "arbiter/entry.py")

# Inject crashes immediately after the real SQLite commit, before its caller can
# deliver the ticket or return the HTTP response. This shim exists only in this test.
CRASH_DAEMON = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from arbiter.cli import main
from arbiter.core import Store
apply = Store.apply
def crash_after_commit(self, action, data):
    result = apply(self, action, data)
    marker = Path("crash-after-" + action)
    if result and marker.is_file():
        marker.unlink()
        os.kill(os.getpid(), signal.SIGKILL)
    return result
Store.apply = crash_after_commit
main()
"""

SHARDS = """
from pathlib import Path
import os, sys, time
start = Path("invocations")
with start.open("a") as log:
    print(os.environ["ARBITER_JOB"], file=log)
for shard in range(3):
    done = Path(f"{shard}.done")
    if done.exists():
        continue
    with Path("writes").open("a") as log:
        print(shard, file=log)
    tmp = Path(f"{shard}.tmp")
    with tmp.open("w") as f:
        f.write("part")
        f.flush()
        if shard == 1 and len(start.read_text().splitlines()) == 1:
            Path("writing").touch()
            while True:
                time.sleep(.1)
        f.write("complete")
    tmp.replace(Path(f"{shard}.out"))
    done.touch()
"""

PROVIDER = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
def send(data):
    print(json.dumps(data), flush=True)
def record(guid, prompt):
    Path(name + ".resumed").write_text(json.dumps([guid, prompt, os.getcwd(), os.environ["ARBITER_SESSION"]]))
for line in sys.stdin:
    request = json.loads(line)
    if name == "claude":
        if request["type"] == "control_request":
            send({"type":"control_response","response":{"request_id":"arbiter-initialize","subtype":"success"}})
        else:
            guid = sys.argv[sys.argv.index("--resume")+1]
            record(guid, request["message"]["content"])
            send({"type":"system","subtype":"init","session_id":guid})
            send({"type":"result","subtype":"success"})
    else:
        method = request["method"]
        if method == "initialize":
            send({"id":request["id"],"result":{}})
        elif method == "thread/resume":
            guid = request["params"]["threadId"]
            send({"id":request["id"],"result":{"thread":{"id":guid}}})
        elif method == "turn/start":
            record(guid, request["params"]["input"][0]["text"])
            send({"id":request["id"],"result":{"turn":{"id":"t"}}})
            send({"method":"turn/completed","params":{"turn":{"status":"completed"}}})
"""


def until(read, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError(f"timed out waiting for {read}")


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="arbiter-recovery-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "state"
        self.state.mkdir(mode=0o700)
        self.log = (self.dir / "daemon.log").open("w+")
        self.addCleanup(self.log.close)
        self.env = {"PATH": os.pathsep.join((str(self.dir), str(ROOT / "bin"), os.environ["PATH"])),
                    "ARBITER_STATE_DIR": str(self.state), "ARBITER_CONFIG": str(self.dir / "config.toml"),
                    "CLAUDE_CONFIG_DIR": str(self.dir / "claude-config"), "CODEX_HOME": str(self.dir / "codex-config")}
        self.guids = {}
        self.seed_database()
        self.port = 0
        self.daemon = None
        self.addCleanup(self.stop)
        self.start()

    def seed_database(self):
        sessions, rows = {}, []
        for kind in ("codex", "claude", "job"):
            guid = str(uuid.uuid4())
            self.guids[kind] = guid
            sessions[kind] = dict(id=kind, name=kind, kind=kind, cwd=str(self.dir), created=time.time(),
                                  state="failed", status="Supervisor failed", preemptible=True, heartbeat=time.time(),
                                  capabilities=["resume-session" if kind == "claude" else "resume-thread"] if kind != "job" else [])
            if kind == "job":
                continue
            if kind == "codex":
                sessions[kind].update(provider=kind, external_id=guid)
                transcript = self.dir / f"codex-config/sessions/2026/01/02/rollout-test-{guid}.jsonl"
            else:
                rows.append((time.time(), "session.status", kind, json.dumps({"text": f"Claude conversation {guid}"})))
                transcript = self.dir / f"claude-config/projects/test/{guid}.jsonl"
            transcript.parent.mkdir(parents=True)
            if kind == "claude":
                sessions[kind]["cwd"] = "."
            transcript.write_text(json.dumps({"sessionId": guid, "cwd": str(self.dir)}) + "\n")
            executable = self.dir / kind
            executable.write_text(PROVIDER)
            executable.chmod(0o700)
        with closing(sqlite3.connect(self.state / "arbiter.sqlite3")) as db, db:
            db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY,body TEXT)")
            db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,kind TEXT,session TEXT,body TEXT)")
            db.execute("INSERT INTO state VALUES(1,?)", (json.dumps({"schema_version": 1, "sessions": sessions}),))
            db.executemany("INSERT INTO events(at,kind,session,body) VALUES(?,?,?,?)", rows)

    def start(self):
        self.daemon = subprocess.Popen([sys.executable, "-I", "-c", CRASH_DAEMON, str(ROOT), "serve", "--port", str(self.port)], env=self.env,
                                       cwd=self.dir, stdout=self.log, stderr=self.log)
        def connected():
            try:
                connection = json.loads((self.state / "connection.json").read_text())
                self.client = Client(**connection)
                return self.client.snapshot()
            except (OSError, RuntimeError, ValueError):
                if self.daemon.poll() is not None:
                    self.fail("temporary daemon could not start: " + (self.dir / "daemon.log").read_text().splitlines()[-1])
                return None
        until(connected)
        self.port = int(self.client.url.rsplit(":", 1)[-1])

    def stop(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.kill()
            self.daemon.wait(timeout=5)

    def restart(self):
        self.stop()
        self.start()

    def cli(self, *args, wait=True):
        cmd = [sys.executable, ENTRY, *args]
        if wait:
            return subprocess.run(cmd, cwd=self.dir, env=self.env, capture_output=True, text=True, timeout=30)
        proc = subprocess.Popen(cmd, cwd=self.dir, env=self.env, stdout=self.log, stderr=self.log)
        self.addCleanup(self.cleanup_process, proc)
        return proc

    @staticmethod
    def cleanup_process(proc):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)

    def sessions(self):
        return self.client.snapshot()["sessions"]

    def named(self, name):
        return next(s for s in self.sessions().values() if s["name"] == name and not s.get("original"))

    def board(self):
        driver = subprocess.Popen(["chromedriver", "--port=0"], stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.addCleanup(self.cleanup_process, driver)
        self.addCleanup(driver.stdout.close)
        for line in driver.stdout:
            match = re.search(r"started successfully on port (\d+)", line)
            if match:
                break
        address = "http://127.0.0.1:" + match[1]
        opener = build_opener(ProxyHandler({}))
        def request(path, data=None, method=None):
            body = None if data is None else json.dumps(data).encode()
            req = Request(address + path, data=body, headers={"Content-Type": "application/json"}, method=method)
            with opener.open(req, timeout=20) as response:
                return json.load(response)["value"]
        session = request("/session", {"capabilities": {"alwaysMatch": {"browserName": "chrome", "goog:chromeOptions":
                          {"binary": "/usr/bin/chromium", "args": ["--headless", "--disable-gpu", "--disable-background-networking",
                           "--no-first-run", f"--user-data-dir={self.dir / 'profile'}"]}}}})["sessionId"]
        prefix = "/session/" + session
        self.addCleanup(request, prefix, None, "DELETE")
        request(prefix + "/url", {"url": self.client.request("/api/launch", {})["url"]})
        def script(js, *args):
            return request(prefix + "/execute/sync", {"script": js, "args": args})
        return script

    def test_documented_example_and_unix_restart(self):
        self.stop()
        socket_path = str(self.dir / "control.sock")
        command = [sys.executable, ENTRY, "serve", "--unix-socket", socket_path]
        def start_unix():
            self.daemon = subprocess.Popen(command, env=self.env, cwd=self.dir, stdout=self.log, stderr=self.log)
            def connected():
                try:
                    connection = json.loads((self.state / "connection.json").read_text())
                    if connection["url"] != "unix://" + socket_path:
                        return False
                    self.client = Client(**connection)
                    return self.client.snapshot()
                except RuntimeError:
                    return False
            until(connected)
        start_unix()
        self.stop()
        start_unix()
        example = (ROOT / "README.md").read_text().split("## Recover a job or conversation", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0]
        env = dict(self.env, PATH=str(ROOT / "bin") + os.pathsep + self.env["PATH"], TMPDIR=str(self.dir))
        result = subprocess.run(["bash", "-c", example], cwd=self.dir, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.named("Publish shards")
        self.assertEqual(source["checkpoint"], "1.done")
        until(lambda: self.sessions()[source["successor"]]["state"] == "finished")
        attempt = self.sessions()[source["successor"]]
        self.assertEqual(attempt["checkpoint"], "2.done")
        self.assertEqual((Path(attempt["cwd"]) / "2.out").read_text(), "output for shard 2\n")

    def test_commit_and_launch_boundaries(self):
        fixture = self.dir / "boundary.py"
        fixture.write_text("from pathlib import Path\nimport sys,time\np=Path('calls')\n"
                           "with p.open('a') as f: print('started',file=f)\n"
                           "if len(p.read_text().splitlines())==1: sys.exit(1)\n"
                           "while not Path('finish').exists(): time.sleep(.05)\n")
        self.assertEqual(self.cli("run", "--name", "boundary", "--record", "--", sys.executable, str(fixture)).returncode, 1)
        sid = self.named("boundary")["id"]
        (self.dir / "crash-after-recover").touch()
        self.assertNotEqual(self.cli("recover", sid).returncode, 0)
        self.daemon.wait(timeout=5)
        (self.dir / "crash-after-dispatch").touch()
        self.start()
        child = self.client.action("recover", session=sid)["id"]
        self.daemon.wait(timeout=8)
        self.assertEqual((self.dir / "calls").read_text().splitlines(), ["started"])
        (self.dir / "crash-after-track").touch()
        self.start()
        self.daemon.wait(timeout=10)
        # The group was committed but the HTTP track acknowledgement never arrived.
        self.assertEqual((self.dir / "calls").read_text().splitlines(), ["started"])
        self.start()
        try:
            until(lambda: (self.dir / "calls").read_text().count("started") == 2)
            self.assertEqual(self.client.action("recover", session=sid)["id"], child)
            self.restart()
            self.assertEqual(self.sessions()[child]["state"], "running")
        finally:
            (self.dir / "finish").touch()
        until(lambda: self.sessions()[child]["state"] == "finished")
        self.assertEqual((self.dir / "calls").read_text().splitlines(), ["started"] * 2)
        self.assertEqual(len([s for s in self.sessions().values() if s.get("original") == sid]), 1)

    def test_outcomes_and_cooperative_handoff(self):
        # Supply a matched kernel journal fixture for a real SIGKILL, without exhausting host memory.
        journal = self.dir / "journalctl"
        journal.write_text("""#!/usr/bin/env python3
from pathlib import Path
import sys
pid = Path('victim').read_text().strip()
if f'Killed process {pid} ' not in sys.argv[-1]:
    sys.exit(1)
print(f'Out of memory: Killed process {pid} (fixture)')
""")
        journal.chmod(0o700)
        for label, script, outcome in [("oom", "echo $$ > victim; kill -9 $$", "oom"),
                                       ("unknown-kill", "kill -9 $$", "failure")]:
            result = self.cli("run", "--name", label, "--auto-retry", "--", "sh", "-c", script)
            self.assertEqual(result.returncode, 137, result.stderr)
            session = self.named(label)
            self.assertEqual((session["outcome"], session["signal"]), (outcome, 9))
            self.assertEqual("retry_at" in session, outcome == "failure")
            if outcome == "failure":
                self.client.action("cancel_retry", session=session["id"])
        wrapper = self.cli("run", "--name", "stop", "--auto-retry", "--", "sleep", "60", wait=False)
        until(lambda: any(s["name"] == "stop" and s.get("groups") for s in self.sessions().values()))
        wrapper.terminate()
        wrapper.wait(timeout=10)
        self.assertEqual(self.named("stop")["outcome"], "cancelled")
        self.assertNotIn("retry_at", self.named("stop"))

        script = ('if test -f handed; then exit 0; fi; touch ready; '
                  'while ! test -f "$ARBITER_YIELD_FILE"; do sleep .1; done; touch handed; exit 75')
        wrapper = self.cli("run", "fixture", "--name", "handoff", "--priority", "25", "--auto-retry", "--preemptible", "--requeue",
                           "--", "sh", "-c", script, wait=False)
        until((self.dir / "ready").exists)
        peer = self.client.action("register", name="peer", pid=os.getpid())
        grant = self.client.action("reserve", session=peer["id"], exclusive=["fixture"])
        until(lambda: self.client.snapshot()["reservations"][grant["id"]]["state"] == "granted")
        self.assertEqual(self.named("handoff")["state"], "waiting")
        self.client.action("finish", session=peer["id"], safe=True)
        self.assertEqual(wrapper.wait(timeout=10), 0)
        session = self.named("handoff")
        self.assertEqual((session["yield_count"], session["retries_used"]), (1, 0))

    def test_migrated_conversations(self):
        self.assertFalse(self.client.snapshot()["settings"]["provider"])
        self.assertIn("No launch record", self.sessions()["job"]["recovery_reason"])
        for kind in ("claude", "codex"):
            self.assertNotEqual(self.cli("rerun-command", kind, "--confirm-unknown").returncode, 0)
            response = self.cli("resume-conversation", kind, "--confirm-unknown")
            self.assertEqual(response.returncode, 0, response.stderr)
            sid = json.loads(response.stdout)["id"]
            until(lambda: self.sessions()[sid]["state"] == "finished")
            guid, prompt, cwd, resumed = json.loads((self.dir / (kind + ".resumed")).read_text())
            self.assertEqual((guid, cwd, resumed), (self.guids[kind], str(self.dir), sid))
            self.assertIn("Supervisor failed", prompt)
        next((self.dir / "codex-config").rglob("*.jsonl")).unlink()
        with self.assertRaisesRegex(RuntimeError, "transcript"):
            self.client.action("recover", session=sid)

    def test_live_recovery(self):
        self.assertFalse(self.client.snapshot()["settings"]["provider"])
        failed = self.cli("run", "--name", "repeatable", "--auto-retry", "--", "sh", "-c", "echo run >> retries; exit 3")
        self.assertEqual(failed.returncode, 3, failed.stderr)
        repeatable = self.named("repeatable")
        self.assertAlmostEqual(repeatable["retry_at"] - repeatable["ended"], 30)
        self.cli("run", "--name", "cancel-me", "--auto-retry", "--", "false")
        cancel = self.named("cancel-me")
        self.assertEqual(self.cli("cancel-retry", cancel["id"]).returncode, 0)

        (self.dir / "shards.py").write_text(SHARDS)
        wrapper = self.cli("run", "device", "--name", "shards", "--record", "--", sys.executable, "shards.py",
                           "private-argument-983", wait=False)
        until((self.dir / "writing").exists)
        original = self.named("shards")
        worker = original["groups"][-1]
        self.addCleanup(lambda: os.killpg(worker, signal.SIGTERM) if system.alive(worker, original["group_starts"][str(worker)]) else None)
        peer = self.client.action("register", name="waiting-peer", pid=os.getpid())
        reservation = self.client.action("reserve", session=peer["id"], exclusive=["device"])
        wrapper.kill()
        wrapper.wait(timeout=5)
        self.restart()
        self.assertEqual(self.sessions()[original["id"]]["state"], "stale")
        self.assertEqual(self.client.snapshot()["reservations"][reservation["id"]]["state"], "requested")
        with self.assertRaisesRegex(RuntimeError, "still run"):
            self.client.action("recover", session=original["id"], confirm_unknown=True)
        os.killpg(worker, signal.SIGTERM)
        until(lambda: self.sessions()[original["id"]]["state"] == "failed")
        self.assertEqual(self.client.snapshot()["reservations"][reservation["id"]]["state"], "granted")

        scoped = self.client.scoped(peer["token"])
        with self.assertRaisesRegex(RuntimeError, "operator token"):
            scoped.action("register", name="no-auto", parent=peer["id"], kind="job", auto_retry=True,
                          launch={"argv": ["true"], "bundle": {}}, cwd=str(self.dir))
        for op in ("recover", "cancel_retry", "dispatch"):
            with self.assertRaises(RuntimeError):
                scoped.action(op, session=original["id"], confirm_unknown=True)
        with self.assertRaises(RuntimeError):
            scoped.request("/api/events")
        self.assertNotIn("private-argument-983", json.dumps(scoped.snapshot()) + json.dumps(self.client.request("/api/events")))
        self.assertNotIn("private-argument-983", json.dumps(self.client.snapshot()))
        board = self.board()
        until(lambda: board("return !!document.getElementById(arguments[0])", original["id"]))
        button = "const row = document.getElementById(arguments[0]); const btn = [...row.querySelectorAll('button')].find(b => b.textContent === 'Rerun command');"
        self.assertTrue(board(button + "return !!btn", original["id"]))
        barrier = threading.Barrier(2)
        def click():
            barrier.wait()
            return board(button + "window.confirm = () => true; btn.click(); return true", original["id"])
        with ThreadPoolExecutor(2) as pool:
            clicked = pool.submit(click)
            barrier.wait()
            response = self.cli("recover", original["id"], "--confirm-unknown")
            self.assertTrue(clicked.result())
        self.assertEqual(response.returncode, 0, response.stderr)
        child = json.loads(response.stdout)["id"]
        self.restart()
        self.assertEqual(self.cli("recover", original["id"]).returncode, 0)
        queued = self.sessions()[child]
        self.assertEqual((queued["predecessor"], queued["state"]), (original["id"], "waiting"))
        self.assertEqual([s["id"] for s in self.sessions().values() if s.get("original") == original["id"]], [child])
        until(lambda: board("return document.getElementById(arguments[0])?.innerText.includes('waiting-peer')", original["id"]))
        self.client.action("finish", session=peer["id"], safe=True)
        until(lambda: self.sessions()[child]["state"] == "finished")
        self.assertEqual((self.dir / "invocations").read_text().splitlines(), [original["id"], child])
        self.assertEqual((self.dir / "writes").read_text().splitlines(), ["0", "1", "1", "2"])
        self.assertEqual([(self.dir / f"{i}.out").read_text() for i in range(3)], ["partcomplete"] * 3)
        self.assertTrue(self.sessions()[cancel["id"]]["retry_cancelled"])
        self.assertNotIn("successor", self.sessions()[cancel["id"]])

        self.cli("run", "--name", "yield", "--auto-retry", "--preemptible", "--requeue", "scratch", "--",
                 "sh", "-c", "if test -f yielded; then exit 0; fi; touch yielded; exit 75")
        yielded = self.named("yield")
        self.assertEqual((yielded["state"], yielded["retries_used"]), ("finished", 0))

        until(lambda: (self.dir / "retries").read_text().count("run") == 3, timeout=175)
        attempts = [s for s in self.sessions().values() if s.get("original") == repeatable["id"]]
        self.assertEqual(len(attempts), 2)
        until(lambda: self.sessions()[attempts[-1]["id"]]["state"] == "failed")
        self.assertEqual(attempts[0]["retries_used"], 1)
        self.assertEqual(attempts[1]["retries_used"], 2)
        self.assertGreaterEqual(attempts[1]["created"] - attempts[0]["ended"], 120)
        self.assertNotIn("retry_at", self.sessions()[attempts[1]["id"]])
        self.assertNotIn("private-argument-983", (self.dir / "daemon.log").read_text())


if __name__ == "__main__":
    unittest.main()
