import json
import http.client
import os
import time
import socket
import urllib.error
import urllib.request
from pathlib import Path


def state_dir():
    return Path(os.environ.get("ARBITER_STATE_DIR", str(Path.home() / ".local/state/arbiter")))


class Client:
    def __init__(self, url=None, token=None):
        self.url = url or os.environ.get("ARBITER_URL")
        self.token = token or os.environ.get("ARBITER_TOKEN")
        if not self.url or not self.token:
            try:
                connection = json.loads((state_dir() / "connection.json").read_text())
            except FileNotFoundError:
                raise RuntimeError("Arbiter is not configured. Start `arbiter serve` first.") from None
            self.url = self.url or connection["url"]
            self.token = self.token or connection["token"]
        # Do not send bearer credentials through environment-configured HTTP proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None):
        raw = json.dumps(body).encode() if body is not None else None
        if self.url.startswith("unix://"):
            conn = UnixConnection(self.url.removeprefix("unix://"))
            try:
                conn.request("POST" if raw is not None else "GET", path, body=raw,
                             headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
                response = conn.getresponse()
                value = json.loads(response.read())
                if response.status >= 400:
                    raise RuntimeError(value.get("error", response.reason))
                return value
            except OSError as e:
                raise RuntimeError(f"Cannot reach Arbiter Unix socket: {e}") from None
            finally:
                conn.close()
        req = urllib.request.Request(self.url + path, data=raw,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("error", str(e))
            except (ValueError, OSError):
                message = str(e)
            raise RuntimeError(message) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach Arbiter at {self.url}: {e.reason}") from None

    def action(self, operation, **data):
        return self.request("/api/action", {"action": operation, "data": data})

    def snapshot(self):
        return self.request("/api/state")

    def inbox(self, sid):
        return self.request("/api/inbox?session=" + sid)

    def wait(self, sid, reservation=None):
        while True:
            self.action("heartbeat", session=sid)
            state = self.snapshot()
            session = state["sessions"][sid]
            if session["state"] in {"finished", "failed"}:
                raise RuntimeError("session ended while waiting")
            # This bundle has never been granted, so the work gated on it has
            # not started. Park locally rather than deadlocking a model inside
            # a reserve tool while waiting for it to acknowledge a pause.
            if reservation and session["state"] == "draining" and state["reservations"][reservation]["state"] == "requested":
                self.action("checkpoint", session=sid, safe=True, status="Paused before resource grant; queued work has not started")
                continue
            if reservation and session["state"] == "draining" and state["reservations"][reservation]["state"] == "granted":
                # Return the held bundle and pause notice to the agent, which
                # must stop work and acknowledge; never silently release it.
                return state
            if session["state"] == "running":
                if not reservation or state["reservations"][reservation]["state"] == "granted":
                    return state
            time.sleep(.5)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
