"""Commands that take turns: run, hold, release, extend, wait, disk claims, and the demo."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

from . import config as configuration
from . import identity, system
from .client import Client, state_dir
from .system import group_alive
from .launch import tracked_popen
from .recovery import OPTIONS

TERMINAL = {"finished", "failed"}
REQUEUE_CODE = 75


class Stopped(BaseException):
    """The wrapper received SIGTERM, SIGHUP or SIGINT."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def catch_signals():
    def handler(signum, frame):
        raise Stopped(signum)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, handler)


def ignore_signals():
    """While cleaning up, a second signal must not abandon the cleanup halfway."""
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)


def duration(value: str) -> float:
    """'90s', '40m', '2h', '1h30m', or a bare number of minutes -> seconds."""
    value = value.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value) * 60
    parts = re.findall(r"(\d+(?:\.\d+)?)([smhd])", value)
    if not parts or "".join(a + b for a, b in parts) != value:
        raise ValueError(f"cannot read duration {value!r}; use e.g. 90s, 40m, 2h, 1h30m")
    return sum(float(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in parts)


def clock(at):
    return time.strftime("%H:%M", time.localtime(at))


def say(text):
    print(f"arbiter: {text}", file=sys.stderr, flush=True)


def register_child(base: Client, kind: str, name: str, **extra) -> tuple[str, Client, str | None]:
    """Register a job/hold under the calling conversation (if any) and return a client scoped to it."""
    parent = identity.current(base)
    data = {"name": name[:100], "kind": kind, "cwd": os.getcwd(), "parent": parent, **extra}
    reg = base.action("register", **{k: v for k, v in data.items() if v is not None})
    return reg["id"], base.scoped(reg["token"]), parent


def bundle(args, label=None, measure=False, disk_gb=0, disk_path=None, eta=None) -> dict:
    data = {"cpu": getattr(args, "cpu", 0) or 0, "memory_mb": getattr(args, "memory_mb", 0) or 0,
            "exclusive": list(dict.fromkeys(getattr(args, "resources", []) + getattr(args, "exclusive", []))),
            "priority": getattr(args, "priority", 0) or 0}
    if label:
        data["label"] = label
    if measure:
        data["measure"] = True
    if disk_gb:
        data.update(disk_gb=disk_gb, disk_path=disk_path)
    if eta:
        data["eta"] = eta
    return data


def needs_bundle(data: dict) -> bool:
    return bool(data["cpu"] or data["memory_mb"] or data["exclusive"] or data.get("measure") or data.get("disk_gb"))


def acquire(client: Client, sid: str, rid: str) -> dict:
    """Block until reservation `rid` is granted, printing why it waits whenever that changes."""
    cursor, shown = None, None
    while True:
        client.action("heartbeat", session=sid)
        state = client.snapshot(wait=cursor)
        cursor = state["cursor"]
        s, r = state["sessions"][sid], state["reservations"][rid]
        if s["state"] in TERMINAL:
            raise RuntimeError(f"session ended while waiting: {s['status']}")
        if s["state"] == "draining":
            # Nothing has started yet, so there is nothing to stop: acknowledge locally.
            client.action("checkpoint", session=sid, safe=True, status="Paused before starting; queued work has not begun")
            if s.get("preempted_by"):
                # The scheduler asked us to hand over before the command even started.
                client.action("requeue", session=sid)
            continue
        if s["state"] == "paused":
            if shown != "paused":
                say("paused by Arbiter; waiting to be resumed")
                shown = "paused"
            continue
        if r["state"] == "granted" and s["state"] == "running":
            return state
        if r["state"] == "requested" and r["reason"] != shown and r["reason"] != "Waiting for scheduler":
            say(f"waiting: {r['reason']}")
            client.action("report", session=sid, status=f"Waiting: {r['reason']}"[:8000])
            shown = r["reason"]


class NoiseMeter:
    """CPU use of processes outside a measured run, sampled once a second."""

    def __init__(self, own_session: int, limit_pct: float):
        self.own, self.limit = own_session, limit_pct
        self.tick = os.sysconf("SC_CLK_TCK")
        self.me = os.getpid()
        self.last, self.at = self.read(), time.monotonic()
        self.over: dict[int, list] = {}

    def read(self) -> dict[int, tuple[int, str]]:
        out = {}
        for pid, f in system.processes():
            try:
                if pid == self.me or int(f[3]) == self.own:
                    continue
                out[pid] = (int(f[11]) + int(f[12]), "")
            except (IndexError, ValueError):
                pass
        return out

    def sample(self):
        now, at = self.read(), time.monotonic()
        dt = max(at - self.at, 1e-3)
        for pid, (ticks, _) in now.items():
            before = self.last.get(pid)
            if before is None:
                continue
            pct = 100 * (ticks - before[0]) / self.tick / dt
            if pct > self.limit:
                entry = self.over.setdefault(pid, [system.comm(pid), 0, 0.0])
                entry[1] += 1
                entry[2] = max(entry[2], pct)
        self.last, self.at = now, at

    def verdict(self, seconds: float) -> tuple[bool, str]:
        need = 1 if seconds < 3 else 2
        noisy = sorted(((v[2], pid, v) for pid, v in self.over.items() if v[1] >= need), reverse=True)
        if not noisy:
            return True, f"CLEAN: no outside process used more than {self.limit:.0f}% of a core"
        top = ", ".join(f"{pid} {v[0]} (up to {pct:.0f}% of a core for ~{v[1]}s)" for pct, pid, v in noisy[:3])
        return False, f"NOISY: {top}"


def wait_quiet(cfg: dict):
    m = cfg["measure"]
    meter = system.CpuMeter()
    deadline, calm = time.monotonic() + m["quiet_timeout_s"], 0
    busy = None
    while time.monotonic() < deadline:
        time.sleep(1)
        busy = meter.sample()
        calm = calm + 1 if busy is not None and busy < m["quiet_pct"] else 0
        if calm >= m["quiet_seconds"]:
            return
    say(f"the machine did not get quiet ({busy or 0:.0f}% busy); measuring anyway")


def run_job(args, argv):
    if not argv:
        raise ValueError("provide a command after --")
    base = Client(patient=60)
    eta = duration(args.eta) if args.eta else None
    data = bundle(args, label=args.label, measure=args.measure, eta=eta)
    name = args.name or args.label or os.path.basename(argv[0])
    caps = ["drain-current-command", "automatic-wakeup", "process-group-tracking"]
    if args.preemptible:
        caps.append("yield-on-request")
    catch_signals()
    extra = {}
    if args.record or args.auto_retry:
        extra["launch"] = {"argv": argv, "bundle": data, **{key: getattr(args, key) for key in OPTIONS}}
        extra["auto_retry"] = args.auto_retry
    sid, client, _ = register_child(base, "job", name, pid=os.getpid(), yields=bool(args.preemptible),
                                    capabilities=caps, **extra)
    return execute_job(args, argv, sid, client, data)


def execute_job(args, argv, sid, client, data, rid=None):
    catch_signals()
    say(f"session {sid}")
    yield_file = state_dir() / "yield" / sid
    proc, code, stopped = None, 1, True
    cancelled, yielded, observed = False, False, False
    started_at = time.time()
    try:
        while True:
            if rid or needs_bundle(data):
                if rid is None:
                    rid = client.action("reserve", session=sid, **data)["id"]
                acquire(client, sid, rid)
            if args.measure:
                wait_quiet(configuration.load())
            env = dict(os.environ, ARBITER_JOB=sid)
            if args.preemptible:
                env["ARBITER_YIELD_FILE"] = str(yield_file)
                yield_file.unlink(missing_ok=True)
            stopped = False
            started_at = time.time()
            proc = tracked_popen(argv, lambda pid: client.action("track", session=sid, pgid=pid, reservation=rid), env=env)
            # Only the program name goes on the board: arguments often carry secrets.
            client.action("report", session=sid, status=f"Running: {args.label or os.path.basename(argv[0])}",
                          stopping_point="Current command and its children finish")
            yielded = supervise(client, sid, proc, args, yield_file)
            stopped = True
            code = proc.returncode
            observed = True
            if args.preemptible and args.requeue and code == REQUEUE_CODE:
                say("command handed over (exit 75); requeueing")
                client.action("handoff", session=sid, reservation=rid)
                observed = False
                continue
            if yielded and code != 0:
                say("command stopped after a yield request")
            break
    except Stopped as e:
        ignore_signals()
        cancelled = True
        try:
            client.action("cancelling", session=sid)
        except (RuntimeError, OSError):
            pass
        code = 128 + e.signum
        if proc and proc.poll() is None:
            # Pass the same signal to the whole command so it can clean up.
            try:
                os.killpg(proc.pid, signal.SIGINT if e.signum == signal.SIGINT else signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        stopped = not proc or (proc.poll() is not None and not group_alive(proc.pid))
    except BaseException:
        ignore_signals()
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        stopped = not proc or (proc.poll() is not None and not group_alive(proc.pid))
        raise
    finally:
        ignore_signals()
        yield_file.unlink(missing_ok=True)
        try:
            if stopped:
                result = proc.returncode if proc else None
                oom = observed and result == -signal.SIGKILL and system.oom_killed(proc.pid, started_at)
                if cancelled:
                    status = "Command cancelled"
                elif oom:
                    status = "Command killed by the OOM killer"
                elif not observed:
                    status = "Wrapper interrupted; outcome unknown"
                elif code < 0:
                    status = f"Command killed by signal {-code}"
                else:
                    status = f"Command exited with code {code}"
                client.action("finish", session=sid, safe=True, failed=code != 0, status=status,
                              returncode=result, cancelled=cancelled, yielded=yielded and code != 0,
                              uncertain=not observed, oom=oom)
            else:
                # Arbiter releases these on its own once the command's processes have all exited.
                say(f"the command is still running; its reservation stays held until it exits ({sid})")
        except (RuntimeError, OSError) as e:
            say(f"could not record the finish ({e}); Arbiter will release it when this process is gone")
    if code:
        # A negative code means the command died from a signal; report the shell convention 128+N.
        sys.exit(code if code > 0 else 128 - code)


def supervise(client, sid, proc, args, yield_file) -> bool:
    """Heartbeat, answer requests and (for --measure) sample noise until the command's group exits."""
    meter = NoiseMeter(proc.pid, configuration.load()["measure"]["noise_core_pct"]) if args.measure else None
    began, last, yielded = time.monotonic(), 0.0, False
    while proc.poll() is None or group_alive(proc.pid):
        time.sleep(1 if meter else .5)
        if meter:
            meter.sample()
        if time.monotonic() - last < 2:
            continue
        last = time.monotonic()
        client.action("heartbeat", session=sid)
        for c in client.inbox(sid)["commands"]:
            if c["action"] == "status":
                client.action("report", session=sid, command=c["id"], status="Command still running",
                              stopping_point="Current command and its children finish")
            elif c["action"] == "pause" and c["state"] == "pending":
                client.action("delivered", session=sid, command=c["id"])
                if args.preemptible:
                    yielded = True
                    yield_file.parent.mkdir(parents=True, exist_ok=True)
                    yield_file.write_text(c["reason"] + "\n")
                    if args.yield_signal:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    client.action("report", session=sid, status=f"Yielding: {c['reason']}",
                                  stopping_point="Command exits (75 hands over and requeues)" if args.requeue else "Command exits")
                else:
                    client.action("report", session=sid, status="Draining current command; no signal sent",
                                  stopping_point="Current command and its children finish")
    proc.wait()
    if meter:
        clean, verdict = meter.verdict(time.monotonic() - began)
        print(verdict, file=sys.stderr, flush=True)
        client.action("report", session=sid, status=f"Measured run {verdict}")
    return yielded


def hold(args):
    base = Client(patient=60)
    eta = duration(args.eta)
    label = args.label or ("measure" if args.measure else " ".join(args.resources) or "hold")
    data = bundle(args, label=args.label, measure=args.measure, eta=eta)
    if not needs_bundle(data):
        raise ValueError("name at least one resource, or --measure")
    catch_signals()
    sid, client, _ = register_child(base, "hold", label, pid=os.getpid())
    granted = False
    try:
        r = client.action("reserve", session=sid, **data)
        state = acquire(client, sid, r["id"])
        # From here the hold lives by --pid, or by its ETA alone.
        client.action("adopt", session=sid, pid=args.pid)
        granted = True
    finally:
        if not granted:
            ignore_signals()
            try:
                client.action("finish", session=sid, safe=True, status="Hold abandoned before it was granted")
            except (RuntimeError, OSError):
                pass
    r = state["reservations"][r["id"]]
    what = " ".join(r["exclusive"] + (["--measure"] if r.get("measure") else [])) or "resources"
    until = f"while process {args.pid} runs (ETA ~{clock(r['eta'])})" if args.pid else f"until ~{clock(r['eta'])}, stale at {clock(r['stale_at'])}"
    print(f"HELD {what} {until} ({sid})")


def claim_disk(args):
    base = Client(patient=60)
    path = os.path.abspath(args.path or os.getcwd())
    data = bundle(args, label=args.label or f"disk {args.gb:g} GB", disk_gb=args.gb, disk_path=path, eta=duration(args.eta))
    catch_signals()
    sid, client, _ = register_child(base, "hold", data["label"], pid=os.getpid())
    granted = False
    try:
        r = client.action("reserve", session=sid, **data)
        state = acquire(client, sid, r["id"])
        client.action("adopt", session=sid, pid=None)
        granted = True
    finally:
        if not granted:
            ignore_signals()
            try:
                client.action("finish", session=sid, safe=True, status="Disk claim abandoned before it was granted")
            except (RuntimeError, OSError):
                pass
    r = state["reservations"][r["id"]]
    print(f"CLAIMED {args.gb:g} GB on the filesystem of {path} until ~{clock(r['eta'])} ({sid})")


def check_disk(args):
    client = Client()
    path = os.path.abspath(args.path or os.getcwd())
    from urllib.parse import quote
    result = client.request(f"/api/disk?path={quote(path)}&gb={args.gb}")
    word = "OK" if result["ok"] else "SHORT"
    print(f"{word} {args.gb:g} GB requested: {result['free_gb']:.1f} GB free, {result['outstanding_gb']:.1f} GB still owed to claims, "
          f"{result['available_gb']:.1f} GB available on the filesystem of {path}")
    if not result["ok"]:
        sys.exit(1)


def mine(base: Client, state: dict, force: bool, predicate) -> list[tuple[dict, dict]]:
    """Active (session, reservation) pairs the caller may release: its own holds, or with force anyone's hold or stale session."""
    me = identity.current(base)
    out = []
    for r in state["reservations"].values():
        if r["state"] not in {"granted", "requested", "suspended"}:
            continue
        s = state["sessions"][r["session"]]
        if s["state"] in TERMINAL or not predicate(r):
            continue
        own = s["kind"] == "hold" and s.get("parent") == me
        if own or (force and (s["kind"] == "hold" or s["state"] == "stale")):
            out.append((s, r))
    return out


def release(args):
    base = Client()
    state = base.snapshot()
    if args.reservation:
        sid = identity.require(base, args.session)
        return print(base.action("release", session=sid, reservation=args.reservation))
    if not (args.resources or args.measure or args.all):
        raise ValueError("name resources to release, or --measure or --all")
    def wanted(r):
        return args.all or (args.measure and r.get("measure")) or bool(set(args.resources) & set(r["exclusive"]))
    found = mine(base, state, args.force, wanted)
    if not found:
        raise ValueError("you hold none of those" + ("" if args.force else "; --force releases someone else's hold"))
    for s, r in found:
        base.action("finish", session=s["id"], safe=True, status="Released" if not args.force else "Released by force")
        print(f"RELEASED {' '.join(r['exclusive'] + (['--measure'] if r.get('measure') else [])) or 'bundle'} ({s['name']}, {s['id']})")


def release_disk(args):
    base = Client()
    found = mine(base, base.snapshot(), args.force, lambda r: bool(r.get("disk_gb")))
    if not found:
        raise ValueError("you have no disk claims")
    if not args.all and len(found) > 1:
        raise ValueError("you have several disk claims; pass --all")
    for s, r in found:
        base.action("finish", session=s["id"], safe=True, status="Disk claim released")
        print(f"RELEASED disk claim of {r['disk_gb']:g} GB ({s['id']})")


def extend(args):
    base = Client()
    state = base.snapshot()
    me = identity.current(base)
    seconds = duration(args.eta)
    hits = 0
    for r in state["reservations"].values():
        s = state["sessions"][r["session"]]
        if r["state"] not in {"granted", "requested"} or s["state"] in TERMINAL:
            continue
        if not (s["id"] == me or (s.get("parent") == me and s["kind"] in {"hold", "job"})):
            continue
        if args.resources and not set(args.resources) & set(r["exclusive"]) and not (args.measure and r.get("measure")):
            continue
        result = base.action("extend", session=s["id"], eta=seconds)
        hits += 1
        print(f"EXTENDED {' '.join(r['exclusive']) or s['name']} until ~{clock(result['eta'])}")
    if not hits:
        raise ValueError("you hold none of those")


def wait(args):
    base = Client(patient=60)
    me = identity.current(base)
    cursor = None
    while True:
        state = base.snapshot(wait=cursor)
        cursor = state["cursor"]
        blocking = []
        held = [r for r in state["reservations"].values() if r["state"] == "granted"]
        def foreign(r):
            s = state["sessions"][r["session"]]
            return not (me and (s["id"] == me or s.get("parent") == me))
        for v in args.resources:
            holders = [r for r in held if v in r["exclusive"] and foreign(r)]
            if len(holders) >= state["resources"].get(v, {}).get("capacity", 1):
                blocking.append(v)
        if args.measure and any(r.get("measure") and foreign(r) for r in held):
            blocking.append("--measure")
        if args.pressure and not state["pressure"]["ok"]:
            blocking.append("--pressure")
        if args.resumed and me and state["sessions"].get(me, {}).get("state") in {"paused", "draining"}:
            blocking.append("--resumed")
        if args.disk:
            from urllib.parse import quote
            path = os.path.abspath(args.path or os.getcwd())
            if not base.request(f"/api/disk?path={quote(path)}&gb={args.disk}")["ok"]:
                blocking.append("--disk")
        if not blocking:
            what = " ".join(args.resources + [f for f, on in (("--measure", args.measure), ("--pressure", args.pressure),
                                                              ("--resumed", args.resumed), ("--disk", args.disk)) if on])
            print(f"FREE {what}".rstrip(), flush=True)
            return


def status_text(state: dict) -> str:
    sessions, res = state["sessions"], state["reservations"]
    lines = []
    active = [r for r in res.values() if r["state"] in {"granted", "requested", "suspended"} and sessions[r["session"]]["state"] not in TERMINAL]
    queue = sorted((r for r in active if r["state"] == "requested"), key=lambda r: r.get("queue_position", 0))
    names = sorted({v for r in active for v in r["exclusive"]} | set(state.get("resources", {})))
    def who(r):
        s = sessions[r["session"]]
        out = s["name"]
        if s.get("parent") and s["parent"] in sessions:
            out = f"{sessions[s['parent']]['name']}/{out}"
        if r.get("label") and r["label"] != s["name"]:
            out += f" ('{r['label']}')"
        if r.get("eta"):
            out += f" until ~{clock(r['eta'])}"
            if r.get("stale_at") and not s.get("pid"):
                out += f" (stale at {clock(r['stale_at'])})"
        elif r.get("granted"):
            out += f" since {clock(r['granted'])}"
        if s["kind"] == "hold":
            out += " [hold]"
        if s["state"] == "stale":
            out += " [stale]"
        return out
    width = max([len(n) + 4 for n in names] + [12])
    for v in names:
        cap = state.get("resources", {}).get(v, {}).get("capacity", 1)
        holders = [r for r in active if v in r["exclusive"] and r["state"] == "granted"]
        waiting = [r for r in queue if v in r["exclusive"]]
        label = f"{v} {len(holders)}/{cap}" if cap > 1 else v
        line = f"{label:<{width}} " + ("; ".join(who(r) for r in holders) or "free")
        if waiting:
            line += "   waiting: " + ", ".join(sessions[r["session"]]["name"] for r in waiting)
        lines.append(line)
    for r in active:
        if r.get("measure"):
            lines.append(f"{'machine':<{width}} measuring: {who(r)}" + ("" if r["state"] == "granted" else " (waiting)"))
        if r.get("disk_gb"):
            lines.append(f"{'disk':<{width}} {who(r)}: {r['disk_gb']:g} GB -> {r['disk_path']}")
    if queue:
        lines.append("\nWaiting reservations:")
        for position, r in enumerate(queue, 1):
            lines.append(f"  {position}. {r['id']}  {who(r)}")
    live = [s for s in sessions.values() if s["state"] not in TERMINAL and s["kind"] not in {"hold"}]
    if live:
        lines.append("")
        for s in sorted(live, key=lambda s: s["created"]):
            lines.append(f"  {s['name']:<28} {s['kind']:<8} {s['state']:<9} {s['id']}  {s['status'][:70]}")
            if s.get("stale_reason"):
                lines.append(f"    {s['stale_reason']}")
            if s.get("predecessor"):
                lines.append(f"    Recovery of {s['predecessor']} (original {s['original']})")
            usage = s.get("context_usage")
            if usage:
                used = usage["input_tokens"]
                window = usage.get("window_tokens")
                amount = f"{used:,} / {window:,} tokens ({100 * used / window:.1f}%)" if window else f"{used:,} tokens"
                lines.append(f"    Context (last input): {amount}")
    for s in sessions.values():
        if s.get("retry_at"):
            lines.append(f"Retry of {s['name']} ({s['id']}) eligible at {clock(s['retry_at'])}.")
        if s.get("stale_check", {}).get("unanswered_since") is not None:
            lines.append(f"Stale blocker needs review: {s['name']} ({s['id']}) has not answered its status check.")
    p = state.get("pressure", {})
    t = state.get("telemetry", {})
    cpu = t.get("cpu_busy")
    lines.append("")
    lines.append(f"now {time.strftime('%H:%M')}   " + (f"CPU {cpu:.0f}%   " if cpu is not None else "") +
                 (f"free RAM {t['available_mb'] / 1024:.1f} GB   " if t.get("available_mb") is not None else "") +
                 f"pressure: {'ok' if p.get('ok', True) else p.get('reason')}")
    minutes = state.get("settings", {}).get("preferred_phase_minutes")
    if minutes:
        lines.append(f"Preferred reservation length: {minutes} minutes (set --eta for each phase)")
    if state.get("config_error"):
        lines.append(f"config error (ignored): {state['config_error']}")
    return "\n".join(lines)


def demo(delay=.4):
    client = Client()
    a = client.action("register", name="Demo · builder", kind="demo", capabilities=["cooperative-pause"])["id"]
    b = client.action("register", name="Demo · tester", kind="demo", capabilities=["cooperative-pause"])["id"]
    print(f"Builder: {a}\nTester:  {b}", flush=True)
    try:
        client.action("reserve", session=a, cpu=.25, exclusive=["demo:workspace"])
        time.sleep(delay)
        client.action("reserve", session=b, cpu=.25, exclusive=["demo:workspace"])
        msg = client.action("message", session=b, to=a, text="Waiting for the workspace. Can you stop after this batch?", topic="demo")
        client.action("ack_message", session=a, message=msg["id"])
        c = client.action("control", session=a, action="status", reason="Demo update request")
        client.action("report", session=a, command=c["id"], status="Build batch complete", stopping_point="Ready to yield workspace")
        time.sleep(delay)
        client.action("control", session=a, action="pause", reason="Let the waiting tests run")
        client.action("checkpoint", session=a, safe=True, status="Batch complete; no resource-using child processes remain")
        time.sleep(delay)
        client.action("control", session=a, action="resume", reason="Resume automatically after tests release the workspace")
        time.sleep(delay)
        client.action("finish", session=b, safe=True, status="Tests complete")
        client.wait(a)
        client.action("report", session=a, status="Resumed; workspace reacquired")
        time.sleep(delay)
        client.action("finish", session=a, safe=True, status="Demo complete")
        print("Verified: contention → status → pause → waiting worker starts → resume waits → resources reacquired.")
    finally:
        state = client.snapshot()
        for sid in (a, b):
            if state["sessions"][sid]["state"] not in {"finished", "failed"}:
                client.action("finish", session=sid, safe=True, status="Demo cleanup")
