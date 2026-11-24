import fcntl
import hmac
import json
import os
import secrets
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import operator, pam, recovery, system, transcripts
from .client import board_location, state_dir
from .config import Config
from .core import PROVIDERS, TERMINAL, Store

system_memory = system.memory

# Everything a session-scoped token may do, and only to its own session. A session token may
# also register child job/hold sessions under itself (see `permitted`).
SESSION_ACTIONS = {"heartbeat", "report", "reserve", "release", "checkpoint", "delivered", "finish",
                   "message", "ack_message", "approval_request", "track", "extend", "requeue", "handoff", "hook", "adopt", "cancelling"}
# What a session may do to the job and hold sessions it registered (e.g. `arbiter release gpu`).
CHILD_ACTIONS = {"finish", "extend", "report"}


def permitted(who, action, data, store=None):
    """Whether a session-scoped principal may perform `action` with `data`. Returns an error or None."""
    if action == "register":
        if data.get("auto_retry"):
            return "operator token required to enable automatic retries"
        if data.get("parent") != who or data.get("kind") not in {"job", "hold"}:
            return "session tokens may only register job or hold sessions under themselves"
        if data.get("provider") is not None:
            return "session tokens may not register provider sessions"
        return None
    if action not in SESSION_ACTIONS:
        return "operator token required for this action"
    if data.get("session") != who:
        child = store.state["sessions"].get(data.get("session")) if store and isinstance(data.get("session"), str) else None
        if action in CHILD_ACTIONS and child and child.get("parent") == who:
            return None
        return "session tokens may only act on their own session"
    return None


class Launcher:
    """Single-use secrets that exchange for the board's browser cookie."""

    def __init__(self):
        self.lock = threading.Lock()
        self.secrets = {}

    def mint(self, ttl=300):
        secret = secrets.token_urlsafe(16)
        with self.lock:
            now = time.monotonic()
            self.secrets = {k: v for k, v in self.secrets.items() if v is None or v > now}
            self.secrets[secret] = None if ttl is None else now + ttl
        return secret

    def consume(self, secret):
        with self.lock:
            expiry = self.secrets.pop(secret, False)
        return expiry is not False and (expiry is None or expiry > time.monotonic())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, token):
        self.store, self.token = store, token
        self.launcher = Launcher()
        self.login_lock = threading.Lock()
        self.next_login = 0
        self.stopping = threading.Event()
        self.telemetry = system_memory()
        self.context_samples = {}
        self.unix = isinstance(address, str)
        if self.unix:
            self.address_family = socket.AF_UNIX
        super().__init__(address, Handler)

    def server_bind(self):
        if self.unix:
            # Create the socket owner-only from the start rather than chmod-ing it afterwards.
            old = os.umask(0o177)
            try:
                socketserver.TCPServer.server_bind(self)
            finally:
                os.umask(old)
            self.server_name, self.server_port = "localhost", 0
        else:
            super().server_bind()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def valid_host(self):
        if self.server.unix:
            return self.headers.get("Host") == "localhost"
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def principal(self):
        """Return "operator", a session ID (session-scoped token), or None."""
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            cookies = SimpleCookie()
        cookie = cookies.get("arbiter")
        if hmac.compare_digest(bearer.encode(), self.server.token.encode()) or (
                cookie and hmac.compare_digest(cookie.value.encode(), self.server.token.encode())):
            return "operator"
        return self.server.store.session_for_token(bearer) if bearer else None

    def authorized(self):
        return self.principal() == "operator"

    def response(self, status, body, content_type="application/json", cookie=False, location=None):
        raw = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if status >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", f"arbiter={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(raw)

    def fail(self, error):
        """Last-resort handler: log server-side, tell the client nothing but that it failed."""
        traceback.print_exception(error, file=sys.stderr)
        if not getattr(self, "streaming", False):
            try:
                self.response(500, {"error": "internal error"})
            except OSError:
                pass

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.fail(e)

    def get(self):
        if not self.valid_host():
            return self.response(403, {"error": "invalid host"})
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/healthz":
            return self.response(200, {"ok": True})
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if url.path in files:
            name, content_type = files[url.path]
            fresh = False
            if url.path == "/":
                fresh = self.server.launcher.consume(query.get("k", [""])[0])
                if not fresh and not self.authorized():
                    if self.server.unix:
                        return self.response(401, {"error": "the board needs TCP mode"})
                    return self.login_page(401)
            return self.response(200, (Path(__file__).parent / "static" / name).read_bytes(), content_type, cookie=fresh)
        who = self.principal()
        if not who:
            return self.response(401, {"error": "authentication required"})
        if url.path == "/api/state":
            if "wait" in query:
                try:
                    wait, timeout = int(query["wait"][0]), min(10.0, max(0.0, float(query.get("timeout", ["5"])[0])))
                except ValueError:
                    return self.response(400, {"error": "invalid wait parameters"})
                self.server.store.wait_for_change(wait, timeout)
            state = self.server.store.snapshot()
            samples = self.server.context_samples
            for session in state["sessions"].values():
                sample = samples.get((session.get("provider"), session.get("external_id")))
                if sample and sample.usage is not None and session["state"] not in TERMINAL:
                    session["context_usage"] = sample.usage
            state["telemetry"] = self.server.telemetry
            state["conversation_viewer"] = who == "operator"
            if who != "operator":
                # Session clients need scheduler state, not the operator's saved prompts.
                state["settings"].pop("instructions", None)
                state["settings"].pop("presets", None)
                state["operator"].pop("instructions", None)
            return self.response(200, state)
        if url.path == "/api/disk":
            try:
                path, gb = query["path"][0], float(query.get("gb", ["0"])[0])
                if not path.startswith("/"):
                    raise ValueError("path must be absolute")
                return self.response(200, self.server.store.disk_check(path, gb))
            except (KeyError, ValueError, OSError) as e:
                return self.response(400, {"error": str(e)})
        if url.path == "/api/inbox":
            sid = query.get("session", [None])[0]
            if who != "operator" and sid != who:
                return self.response(403, {"error": "session tokens may only read their own inbox"})
            try:
                return self.response(200, self.server.store.inbox(sid))
            except ValueError as e:
                return self.response(400, {"error": str(e)})
        if who != "operator":
            return self.response(403, {"error": "operator token required"})
        if url.path == "/api/conversation":
            try:
                before = int(query["before"][0]) if "before" in query else None
                if before is not None and not 0 < before <= 2**63 - 1:
                    raise ValueError
            except ValueError:
                return self.response(400, {"error": "Invalid conversation cursor"})
            with self.server.store.lock:
                session = self.server.store.state["sessions"].get(query.get("session", [""])[0], {})
                provider, external_id = session.get("provider"), session.get("external_id")
            page = transcripts.conversation(provider, external_id, before)
            if page is None:
                return self.response(404, {"error": "No local conversation log is available for this session."})
            return self.response(200, page)
        if url.path in {"/api/events", "/events"}:
            try:
                cursor = max(0, int(query.get("after", ["0"])[0]), int(self.headers.get("Last-Event-ID", "0")))
            except ValueError:
                return self.response(400, {"error": "invalid event cursor"})
            if url.path == "/api/events":
                return self.response(200, self.server.store.events(cursor))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.streaming = True
            while not self.server.stopping.is_set():
                events = self.server.store.events(cursor)
                for event in events:
                    self.wfile.write(f"id: {event['id']}\ndata: {json.dumps(event)}\n\n".encode())
                    cursor = event["id"]
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                self.server.stopping.wait(1)
            return
        self.response(404, {"error": "not found"})

    def do_POST(self):
        try:
            self.post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.fail(e)

    def post(self):
        origin = self.headers.get("Origin")
        if not self.valid_host() or (origin and origin != "http://" + self.headers.get("Host", "")):
            return self.response(403, {"error": "cross-origin writes refused"})
        if self.path == "/login":
            if self.server.unix or not origin:
                return self.response(403, {"error": "sign-in requires a same-origin browser request"})
            return self.login()
        who = self.principal()
        if not who:
            return self.response(401, {"error": "authentication required"})
        if self.path == "/api/launch":
            if who != "operator":
                return self.response(403, {"error": "operator token required"})
            if self.server.unix:
                return self.response(400, {"error": "the board needs TCP mode; restart without --unix-socket"})
            return self.response(200, {"url": f"http://127.0.0.1:{self.server.server_port}/?k={self.server.launcher.mint()}"})
        if self.path != "/api/action":
            return self.response(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            # A settings edit includes both the expected and proposed preset library.
            limit = 4 * 1024 * 1024 if who == "operator" else 65536
            if not 0 < length <= limit:
                raise ValueError(f"body must be 1–{limit} bytes")
            d = json.loads(self.rfile.read(length))
            if not isinstance(d, dict) or not isinstance(d.get("data", {}), dict):
                raise ValueError("expected JSON object with action and data")
            action = d.get("action")
            if length > 65536 and action != "save_settings":
                raise ValueError("body must be 1–65536 bytes")
            if not isinstance(action, str) or action in {"reconcile", "tick", "operator_decision", "operator_events_reviewed", "dispatch", "auto_recover", "retry_blocked"}:
                raise ValueError("internal action")
            if who != "operator":
                refusal = permitted(who, action, d.get("data", {}), self.server.store)
                if refusal:
                    return self.response(403, {"error": refusal})
            result = self.server.store.apply(action, d.get("data", {}))
            return self.response(200, result)
        except (ValueError, TypeError, KeyError) as e:
            return self.response(400, {"error": str(e)})

    def login_page(self, status, message=""):
        page = (Path(__file__).parent / "static" / "login.html").read_bytes()
        return self.response(status, page.replace(b"<!-- feedback -->", message.encode()), "text/html; charset=utf-8")

    def login(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/x-www-form-urlencoded" or not 0 < length <= 8192:
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode("ascii"), strict_parsing=True,
                              errors="strict", max_num_fields=2)
            username, = fields["username"]
            password, = fields["password"]
        except (ValueError, KeyError):
            return self.login_page(400, "Enter your Linux username and password.")

        if not self.server.login_lock.acquire(blocking=False):
            return self.login_page(429, "A sign-in attempt is in progress. Try again shortly.")
        try:
            status, message = self.verify_login(username, password)
        finally:
            self.server.login_lock.release()
        if status == 303:
            return self.response(status, b"", "text/html", cookie=True, location="/")
        return self.login_page(status, message)

    def verify_login(self, username, password):
        if time.monotonic() < self.server.next_login:
            return 429, "Wait five seconds between sign-in attempts."
        try:
            if pam.authenticate(username, password):
                return 303, ""
            return 401, "Sign-in failed. Check your Linux username and password."
        except OSError:
            return 503, "Linux sign-in is unavailable on this machine."
        finally:
            self.server.next_login = time.monotonic() + 5


def refresh_context(server):
    with server.store.lock:
        conversations = {(s["provider"], s["external_id"]) for s in server.store.state["sessions"].values()
                         if s["state"] not in TERMINAL and s.get("provider") in PROVIDERS
                         and transcripts.valid_id(s.get("external_id"))}
    samples = {}
    for key in conversations:
        try:
            sample = transcripts.sample_context(*key, previous=server.context_samples.get(key))
        except OSError:
            continue
        if sample is not None:
            samples[key] = sample
    server.context_samples = samples


def monitor_context(server):
    while not server.stopping.is_set():
        refresh_context(server)
        server.stopping.wait(15)


def write_private(path, content):
    """Create a file readable only by the owner from the first byte (no chmod-after-write window)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    tmp.replace(path)


def serve(args):
    if args.headroom_mb < 0:
        raise ValueError("memory headroom must be nonnegative")
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock = (directory / "daemon.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        try:
            url = json.loads((directory / "connection.json").read_text())["url"]
        except (OSError, KeyError, ValueError):
            raise RuntimeError("Arbiter has the daemon lock but its address is not available yet") from None
        print(f"Arbiter is already running.\n{board_location(url)}")
        return
    meter = system.CpuMeter()
    memory = system_memory()
    config = Config()
    if config.error:
        print(f"arbiter: ignoring invalid config: {config.error}", file=sys.stderr)
    store = Store(directory / "arbiter.sqlite3", args.cpu or max(1, (os.cpu_count() or 2) * .8),
                  args.memory_mb or max(256, int(memory.get("total_mb", 8192) * .7)), headroom_mb=args.headroom_mb,
                  retention_days=args.retention_days or None, config=config,
                  initial_telemetry=dict(memory, cpu_busy=meter.sample(), gpu_pressure=system.gpu_pressure()))
    connection = directory / "connection.json"
    # Keep the operator token across restarts so running wrappers and supervisors, which read it
    # once, can reconnect to a restarted daemon instead of failing authentication.
    previous = {}
    try:
        previous = json.loads(connection.read_text())
    except (OSError, ValueError):
        pass
    token = previous.get("token")
    token = token if isinstance(token, str) and len(token) >= 32 else secrets.token_urlsafe(32)
    address = str(Path(args.unix_socket).resolve()) if args.unix_socket else ("127.0.0.1", args.port)
    if args.unix_socket and previous.get("url") == "unix://" + address:
        # SIGKILL leaves a socket inode behind. Under the state-directory lock,
        # reclaim our old address only if there is no listener.
        path = Path(address)
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(address)
        except ConnectionRefusedError:
            path.unlink()
        except FileNotFoundError:
            pass
    server = Server(address, store, token)
    url = "unix://" + address if args.unix_socket else f"http://127.0.0.1:{server.server_port}"
    write_private(connection, json.dumps({"url": url, "token": token}))

    def monitor():
        children = []
        while not server.stopping.wait(3):
            server.telemetry = system_memory()
            busy = meter.sample()
            server.telemetry["cpu_busy"] = busy
            gpu_pressure = system.gpu_pressure()
            try:
                store.apply("tick", {"available_mb": server.telemetry.get("available_mb"), "cpu_busy": busy,
                                     "gpu_pressure": gpu_pressure})
                recovery.dispatch(store, url, children)
            except Exception as e:  # never let one bad tick stop liveness checks
                traceback.print_exception(e, file=sys.stderr)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    context_thread = threading.Thread(target=monitor_context, args=(server,), daemon=True)
    context_thread.start()
    operator_thread = threading.Thread(target=operator.run, args=(store, server.stopping), daemon=True)
    operator_thread.start()
    print(f"Arbiter listening at {url}", flush=True)
    if not args.unix_socket:
        print(f"Sign in to the board at {url}/\n"
              f"One-time launch link: {url}/?k={server.launcher.mint(ttl=None)}", flush=True)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        with store.changed:
            store.changed.notify_all()
        monitor_thread.join(timeout=5)
        context_thread.join()
        operator_thread.join(timeout=5)
        server.server_close()
        with store.lock:
            store.db.close()
        if args.unix_socket:
            Path(address).unlink(missing_ok=True)
        # connection.json stays (owner-only) so clients wait for a restart rather than giving up.
        lock.close()
