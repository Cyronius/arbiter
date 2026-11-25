import json
import http.client
import os
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def state_dir():
    if override := os.environ.get("ARBITER_STATE_DIR"):
        return Path(override)
    if os.name == "nt":
        from .config import app_directory
        return app_directory() / "state"
    return Path.home() / ".local/state/arbiter"


def read_connection(directory):
    path = directory / "connection.json"
    if os.name == "nt":
        from .win32 import open_shared_file
        with open_shared_file(path) as file:
            return json.load(file)
    return json.loads(path.read_text(encoding="utf-8"))


def board_location(url):
    if url.startswith("unix://"):
        return f"Listening at {url} (CLI only)"
    return f"Board: {url.rstrip('/')}/"


class Unreachable(RuntimeError):
    """The daemon refused the connection or its socket is gone; the request never arrived."""


class Disconnected(RuntimeError):
    """The transport closed after a request might have arrived."""


class Client:
    def __init__(self, url=None, token=None, patient=0, timeout=10):
        # `patient` is how many seconds to keep retrying while the daemon is down (e.g. restarting).
        # After an ambiguous disconnect, replay only reads and idempotent actions.
        self.patient = patient
        self.timeout = timeout
        self.url = url or os.environ.get("ARBITER_URL")
        self.token = token or os.environ.get("ARBITER_TOKEN")
        self.from_file = not self.url or not self.token
        if self.from_file:
            deadline = time.monotonic() + patient
            while True:
                try:
                    connection = read_connection(state_dir())
                    break
                except (FileNotFoundError, ValueError):
                    if time.monotonic() + 1 > deadline:
                        raise RuntimeError("Arbiter is not running. Start it with `arbiter serve` "
                                           "(or `arbiter install` for a background service).") from None
                    time.sleep(1)
            self.url = self.url or connection["url"]
            self.token = self.token or connection["token"]
        # Do not send bearer credentials through environment-configured HTTP proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def scoped(self, token):
        """A client acting with a session-scoped token against the same daemon."""
        return Client(url=self.url, token=token, patient=self.patient, timeout=self.timeout)

    def board_link(self, *, tab=None):
        link = self.request("/api/launch", {})["url"]
        if tab:
            link += "&" + urllib.parse.urlencode({"tab": tab})
        return link

    def request(self, path, body=None, timeout=None):
        timeout = timeout or self.timeout
        deadline = time.monotonic() + self.patient
        delay = .5
        while True:
            try:
                return self.request_once(path, body, timeout)
            except (Unreachable, Disconnected) as error:
                if isinstance(error, Disconnected) and body is not None and body.get("action") not in {"heartbeat", "track", "recover", "cancel_retry"}:
                    raise
                if time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 5)

    def request_once(self, path, body, timeout):
        raw = json.dumps(body).encode() if body is not None else None
        if self.url.startswith("unix://"):
            if os.name == "nt":
                raise RuntimeError("Unix socket connections are available on Linux. Use localhost TCP on Windows.")
            conn = UnixConnection(self.url.removeprefix("unix://"), timeout)
            try:
                conn.request("POST" if raw is not None else "GET", path, body=raw,
                             headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
                response = conn.getresponse()
                value = json.loads(response.read())
                if response.status >= 400:
                    raise RuntimeError(value.get("error", response.reason))
                return value
            except (ConnectionRefusedError, FileNotFoundError) as e:
                raise Unreachable(f"Cannot reach Arbiter Unix socket: {e}") from None
            except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected) as e:
                raise Disconnected("Arbiter disconnected during the request") from e
            except OSError as e:
                raise RuntimeError(f"Cannot reach Arbiter Unix socket: {e}") from None
            finally:
                conn.close()
        req = urllib.request.Request(self.url + path, data=raw,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            with e:
                try:
                    message = json.load(e).get("error", str(e))
                except (ValueError, OSError):
                    message = str(e)
            raise RuntimeError(message) from None
        except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected) as e:
            raise Disconnected("Arbiter disconnected during the request") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionRefusedError, FileNotFoundError)):
                raise Unreachable(f"Cannot reach Arbiter at {self.url}: {e.reason}") from None
            if isinstance(e.reason, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
                raise Disconnected("Arbiter disconnected during the request") from e
            raise RuntimeError(f"Cannot reach Arbiter at {self.url}: {e.reason}") from None

    def action(self, operation, **data):
        return self.request("/api/action", {"action": operation, "data": data})

    def snapshot(self, wait=None):
        """Read state. With `wait`=an event cursor, block (server-side, up to 5s) until something newer happens."""
        if wait is None:
            return self.request("/api/state")
        return self.request(f"/api/state?wait={int(wait)}&timeout=5", timeout=15)

    def inbox(self, sid):
        return self.request("/api/inbox?session=" + sid)

    def wait(self, sid, reservation=None):
        cursor = None
        while True:
            self.action("heartbeat", session=sid)
            state = self.snapshot(wait=cursor)
            cursor = state["cursor"]
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


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
