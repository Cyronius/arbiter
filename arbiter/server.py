import fcntl
import hmac
import json
import os
import secrets
import socket
import socketserver
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .client import state_dir
from .core import Store


def system_memory():
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return {"total_mb": int(info["MemTotal"].split()[0]) // 1024,
                "available_mb": int(info["MemAvailable"].split()[0]) // 1024,
                "load": list(os.getloadavg())}
    except (OSError, KeyError, ValueError):
        return {}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, token):
        self.store, self.token = store, token
        self.stopping = threading.Event()
        self.telemetry = system_memory()
        self.unix = isinstance(address, str)
        if self.unix:
            self.address_family = socket.AF_UNIX
        super().__init__(address, Handler)

    def server_bind(self):
        if self.unix:
            socketserver.TCPServer.server_bind(self)
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

    def authorized(self):
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        cookie = cookies.get("arbiter")
        return hmac.compare_digest(bearer, self.server.token) or bool(cookie and hmac.compare_digest(cookie.value, self.server.token))

    def response(self, status, body, content_type="application/json", cookie=False):
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
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            return self.response(200, (Path(__file__).parent / "static" / name).read_bytes(), content_type, cookie=url.path == "/")
        if not self.authorized():
            return self.response(401, {"error": "authentication required"})
        if url.path == "/api/state":
            state = self.server.store.snapshot()
            state["telemetry"] = self.server.telemetry
            return self.response(200, state)
        if url.path == "/api/inbox":
            try:
                return self.response(200, self.server.store.inbox(query.get("session", [None])[0]))
            except ValueError as e:
                return self.response(400, {"error": str(e)})
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

    def post(self):
        origin = self.headers.get("Origin")
        if not self.valid_host() or (origin and origin != "http://" + self.headers.get("Host", "")):
            return self.response(403, {"error": "cross-origin writes refused"})
        if not self.authorized():
            return self.response(401, {"error": "authentication required"})
        if self.path != "/api/action":
            return self.response(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("body must be 1–65536 bytes")
            d = json.loads(self.rfile.read(length))
            if not isinstance(d, dict) or not isinstance(d.get("data", {}), dict):
                raise ValueError("expected JSON object with action and data")
            action = d.get("action")
            if action in {"reconcile", "tick"}:
                raise ValueError("internal action")
            result = self.server.store.apply(action, d.get("data", {}))
            return self.response(200, result)
        except (ValueError, TypeError, KeyError) as e:
            return self.response(400, {"error": str(e)})


def serve(args):
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock = (directory / "daemon.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("An Arbiter daemon already owns this state directory") from None
    memory = system_memory()
    store = Store(directory / "arbiter.sqlite3", args.cpu or max(1, (os.cpu_count() or 2) * .8),
                  args.memory_mb or max(256, int(memory.get("total_mb", 8192) * .7)), headroom_mb=args.headroom_mb)
    store.available_mb = memory.get("available_mb")
    token = secrets.token_urlsafe(32)
    address = str(Path(args.unix_socket).resolve()) if args.unix_socket else ("127.0.0.1", args.port)
    if args.headroom_mb < 0:
        raise ValueError("memory headroom must be nonnegative")
    server = Server(address, store, token)
    if args.unix_socket:
        os.chmod(address, 0o600)
    url = "unix://" + address if args.unix_socket else f"http://127.0.0.1:{server.server_port}"
    connection = directory / "connection.json"
    tmp = directory / "connection.tmp"
    tmp.write_text(json.dumps({"url": url, "token": token}))
    os.chmod(tmp, 0o600)
    tmp.replace(connection)

    def monitor():
        while not server.stopping.wait(3):
            server.telemetry = system_memory()
            store.apply("tick", {"available_mb": server.telemetry.get("available_mb")})

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    print(f"Arbiter listening at {url}", flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        monitor_thread.join(timeout=5)
        server.server_close()
        with store.lock:
            store.db.close()
        if args.unix_socket:
            Path(address).unlink(missing_ok=True)
        lock.close()
