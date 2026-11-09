import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .client import Client


def group_alive(pgid):
    """Ignore zombies; inspect the entire launched process group, not just its leader."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            pass
    return False


def run_job(args):
    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    if not argv:
        raise ValueError("provide a command after --")
    client = Client(patient=60)
    sid = client.action("register", name=args.name, kind="job", cwd=os.getcwd(),
                        capabilities=["drain-current-command", "automatic-wakeup", "process-group-tracking"])["id"]
    print(f"Arbiter session: {sid}", file=sys.stderr, flush=True)
    proc = None
    stopped = True
    code = 1
    try:
        if args.cpu or args.memory_mb or args.exclusive:
            r = client.action("reserve", session=sid, cpu=args.cpu, memory_mb=args.memory_mb,
                              exclusive=args.exclusive, priority=args.priority)
            cursor = None
            while True:
                client.action("heartbeat", session=sid)
                state = client.snapshot(wait=cursor)
                cursor = state["cursor"]
                if state["sessions"][sid]["state"] == "draining":
                    client.action("checkpoint", session=sid, safe=True, status="Paused before command launch")
                    cursor = None
                    continue
                if state["sessions"][sid]["state"] == "running" and state["reservations"][r["id"]]["state"] == "granted":
                    break
        proc = subprocess.Popen(argv, start_new_session=True)
        stopped = False
        # Only the program name goes on the board: arguments often carry secrets.
        client.action("report", session=sid, status=f"Running: {os.path.basename(argv[0])} ({len(argv) - 1} arguments)", stopping_point="Current command and its children finish")
        last = 0
        while proc.poll() is None or group_alive(proc.pid):
            time.sleep(.5)
            if time.monotonic() - last < 2:
                continue
            last = time.monotonic()
            client.action("heartbeat", session=sid)
            for c in client.inbox(sid)["commands"]:
                if c["action"] == "status":
                    client.action("report", session=sid, command=c["id"], status="Command still running", stopping_point="Current command and its children finish")
                elif c["action"] == "pause" and c["state"] == "pending":
                    client.action("delivered", session=sid, command=c["id"])
                    client.action("report", session=sid, status="Draining current command; no signal sent", stopping_point="Current command and its children finish")
        stopped = True
        code = proc.wait()
    except BaseException:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        stopped = not proc or (proc.poll() is not None and not group_alive(proc.pid))
        raise
    finally:
        if stopped:
            client.action("finish", session=sid, safe=True, failed=code != 0, status=f"Command exited with code {code}")
        else:
            print(f"Child processes remain. Reservations retained for {sid}.", file=sys.stderr)
    if code:
        # A negative code means the command died from a signal; report the shell convention 128+N.
        sys.exit(code if code > 0 else 128 - code)


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
