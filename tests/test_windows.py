"""Windows daemon and Job Object integration tests."""
import http.client
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from arbiter.client import Client, Unreachable, read_connection
from arbiter.system import alive, group_alive

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "arbiter" / "entry.py"


@unittest.skipUnless(os.name == "nt", "Windows daemon and Job Objects")
class WindowsIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="arbiter windows ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.log = (self.root / "daemon.log").open("w+", encoding="utf-8")
        self.addCleanup(self.log.close)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ARBITER_") and key not in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")}
        self.env.update(ARBITER_STATE_DIR=str(self.state), ARBITER_CONFIG=str(self.root / "config.toml"))

    def start(self, *args):
        proc = subprocess.Popen([sys.executable, str(ENTRY), *args], cwd=self.root, env=self.env,
                                stdout=self.log, stderr=self.log, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def until(self, check, *, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if value := check():
                    return value
            except (FileNotFoundError, PermissionError, Unreachable):
                pass
            time.sleep(.05)
        self.log.seek(0)
        self.fail(f"Timed out waiting for {check.__name__}\n{self.log.read()}")

    def test_board_and_orphaned_work_survive_daemon_restart(self):
        daemon = self.start("serve", "--port", "0")

        def check_connection():
            info = read_connection(self.state)
            client = Client(**info, timeout=2)
            client.snapshot()
            return client

        client = self.until(check_connection)
        port = urlsplit(client.url).port
        self.assertEqual(self.start("serve").wait(timeout=5), 0)
        self.assertGreater(client.snapshot()["telemetry"]["available_mb"], 0)
        disk_path = quote(str(self.root / "future output"))
        self.assertTrue(client.request(f"/api/disk?path={disk_path}&gb=0")["ok"])
        launch = urlsplit(client.board_link(tab="settings"))
        self.assertEqual(parse_qs(launch.query)["tab"], ["settings"])
        board = http.client.HTTPConnection(launch.hostname, launch.port, timeout=2)
        self.addCleanup(board.close)
        board.request("GET", "/?" + launch.query)
        response = board.getresponse()
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        board.request("GET", "/", headers={"Cookie": cookie})
        response = board.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        board.close()

        job = self.root / "job.py"
        job.write_text(
            "import os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "first, release, replay = map(Path, ('first', 'release', 'replay'))\n"
            "if len(sys.argv) == 1:\n"
            "    first.touch()\n"
            "    deadline = time.monotonic() + 25\n"
            "    while not release.exists() and time.monotonic() < deadline: time.sleep(.05)\n"
            "elif first.exists():\n"
            "    replay.write_text(sys.argv[1], encoding='utf-8')\n"
            "else:\n"
            "    Path('parent_pid').write_text(str(os.getpid()))\n"
            "    subprocess.Popen([sys.executable, __file__], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n",
            encoding="utf-8",
        )
        self.addCleanup((self.root / "release").touch)
        wrapper = self.start("run", "--record", "fixture", "--", sys.executable, str(job), "arguments α & β")
        self.until((self.root / "first").exists)
        attempt, = client.snapshot()["sessions"].values()
        sid = attempt["id"]
        group, = attempt["groups"]
        started = attempt["group_starts"][str(group)]
        parent = int((self.root / "parent_pid").read_text())
        self.until(lambda: not alive(parent))
        self.assertTrue(group_alive(group, started))
        self.assertIsNone(wrapper.poll())
        self.stop(wrapper)
        self.until(lambda: client.snapshot()["sessions"][sid].get("worker_survived"))
        self.stop(daemon)
        self.start("serve", "--port", str(port))
        restarted = self.until(check_connection)
        self.assertEqual(restarted.token, client.token)
        self.assertTrue(restarted.snapshot()["sessions"][sid]["worker_survived"])
        self.assertTrue(group_alive(group, started))

        (self.root / "release").touch()
        self.until(lambda: restarted.snapshot()["sessions"][sid].get("outcome") == "unknown")
        followup = restarted.action("recover", session=sid, confirm_unknown=True)["id"]
        self.until(lambda: restarted.snapshot()["sessions"][followup]["state"] == "finished")
        recovered = restarted.snapshot()["sessions"][followup]
        self.until(lambda: not alive(recovered["pid"], recovered["pid_start"]))
        self.assertEqual((self.root / "replay").read_text(encoding="utf-8"), "arguments α & β")
