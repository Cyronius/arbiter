import argparse
import json
import os
import sys

from .client import Client


def output(value):
    print(json.dumps(value, indent=2))


def resources(p, positional=False):
    if positional:
        p.add_argument("resources", nargs="*", metavar="RESOURCE", help="Resource names, e.g. gpu npu network port-3000")
    p.add_argument("-x", "--exclusive", action="append", default=[], help="A resource name (repeatable)")
    p.add_argument("--cpu", type=float, default=0, help="CPU scheduling weight")
    p.add_argument("--memory-mb", type=float, default=0, help="Job memory in MiB")
    p.add_argument("--priority", type=float, default=0)


def session_option(p):
    p.add_argument("--session", "-s", help="Session ID (default: the Claude Code/Codex session you run in)")


def parser():
    p = argparse.ArgumentParser(prog="arbiter", description="Take turns with shared resources on this machine; coordinate agent sessions")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="Run the localhost daemon")
    s.add_argument("--port", type=int, default=7483)
    s.add_argument("--unix-socket", help="Serve local HTTP over a Unix socket instead of TCP (CLI/MCP only)")
    s.add_argument("--cpu", type=float, help="Concurrent CPU scheduling budget (default: 80%% of logical CPUs)")
    s.add_argument("--memory-mb", type=int, help="Job memory budget in MiB (default: 70%% of RAM)")
    s.add_argument("--headroom-mb", type=int, default=512, help="Keep this much measured free memory (default: 512 MiB)")
    s.add_argument("--retention-days", type=float, default=14, help="Forget sessions finished longer ago than this (0 keeps everything; default: 14)")
    i = sub.add_parser("install", help="Install the service, the hooks for existing Claude Code and Codex sessions, and skills")
    i.add_argument("--no-service", action="store_true", help="Do not install or start the systemd user service")
    i.add_argument("--no-hooks", action="store_true", help="Do not install Claude Code and Codex hooks")
    i.add_argument("--no-skills", action="store_true", help="Do not install the arbiter skill")
    sub.add_parser("uninstall", help="Remove what `arbiter install` added (keeps state and config)")
    sub.add_parser("open", help="Open the browser board with a fresh one-time link")
    st = sub.add_parser("status", help="Who holds what, until when, and who waits")
    st.add_argument("--json", action="store_true", help="Print the full state as JSON")
    sub.add_parser("whoami", help="Print the Arbiter session of the agent session you run in")
    e = sub.add_parser("events", help="Read durable message-board events")
    e.add_argument("--after", type=int, default=0)

    run = sub.add_parser("run", help="Run a command holding resources; they free when it exits",
                         usage="arbiter run [options] [RESOURCE ...] -- COMMAND ...")
    resources(run, positional=True)
    run.add_argument("--name", help="Name on the board (default: label or program name)")
    run.add_argument("--label", help="What the run is for, shown to anyone waiting")
    run.add_argument("--eta", help="Expected duration, e.g. 40m (shown to anyone waiting)")
    run.add_argument("--measure", action="store_true", help="Take the whole machine, wait until it is quiet, print CLEAN or NOISY")
    run.add_argument("--preemptible", action="store_true", help="When another session waits for these resources, create $ARBITER_YIELD_FILE")
    run.add_argument("--yield-signal", action="store_true", help="With --preemptible, also send SIGTERM to the command")
    run.add_argument("--requeue", action="store_true", help="With --preemptible, exit code 75 hands over, then reruns the command")

    run.add_argument("--record", action="store_true", help="Privately save this command for a manual rerun")
    run.add_argument("--auto-retry", action="store_true", help="Record a repeatable command and retry observed failures twice")
    recover = sub.add_parser("recover", aliases=["rerun-command", "resume-conversation"],
                             help="Rerun a recorded command or resume a managed conversation")
    recover.add_argument("session", help="Predecessor attempt ID")
    recover.add_argument("--confirm-unknown", action="store_true", help="Replay after inspecting possible side effects")
    cancel = sub.add_parser("cancel-retry", help="Cancel an automatic retry during backoff")
    cancel.add_argument("session")

    h = sub.add_parser("hold", help="Keep resources across several commands until `arbiter release`")
    resources(h, positional=True)
    h.add_argument("--eta", required=True, help="Expected duration, e.g. 45m; an unattended hold lapses some time after it")
    h.add_argument("--label", help="What the hold is for")
    h.add_argument("--pid", type=int, help="Keep the hold while this process runs, past the ETA")
    h.add_argument("--measure", action="store_true", help="Hold the whole machine for a series of measurements")
    rel = sub.add_parser("release", help="End your hold (or, with --force, someone else's stuck hold)")
    rel.add_argument("resources", nargs="*", metavar="RESOURCE")
    rel.add_argument("--measure", action="store_true")
    rel.add_argument("--all", action="store_true", help="Release all your holds")
    rel.add_argument("--force", action="store_true", help="Release a hold or stale session that is not yours")
    rel.add_argument("--reservation", help="Release one reservation by ID (manual integrations)")
    session_option(rel)
    ex = sub.add_parser("extend", help="Push out the ETA of your holds and runs")
    ex.add_argument("resources", nargs="*", metavar="RESOURCE")
    ex.add_argument("--measure", action="store_true")
    ex.add_argument("--eta", required=True)
    w = sub.add_parser("wait", help="Block until resources are free, then print one FREE line")
    w.add_argument("resources", nargs="*", metavar="RESOURCE")
    w.add_argument("--measure", action="store_true", help="Until nobody is measuring")
    w.add_argument("--pressure", action="store_true", help="Until CPU and memory pressure ease")
    w.add_argument("--resumed", action="store_true", help="Until this session is no longer paused")
    w.add_argument("--disk", type=float, metavar="GB", help="Until this much disk is available after claims")
    w.add_argument("--path", help="Filesystem for --disk (default: current directory)")
    d = sub.add_parser("disk", help="Claim disk space for a large download")
    dsub = d.add_subparsers(dest="disk_command", required=True)
    dc = dsub.add_parser("claim", help="Claim GB on a filesystem; waits until it is available")
    dc.add_argument("gb", type=float)
    dc.add_argument("--eta", required=True)
    dc.add_argument("--path", help="Where the data will go (default: current directory)")
    dc.add_argument("--label")
    dk = dsub.add_parser("check", help="Is this much space available after outstanding claims?")
    dk.add_argument("gb", type=float)
    dk.add_argument("--path")
    dr = dsub.add_parser("release", help="End your disk claim")
    dr.add_argument("--all", action="store_true")
    dr.add_argument("--force", action="store_true")

    r = sub.add_parser("register", help="Register an independently managed session")
    r.add_argument("name")
    r.add_argument("--kind", default="external", choices=["external", "codex", "claude", "demo"])
    r.add_argument("--pid", type=int, help="Owner process: the session ends (and releases) when it exits")
    hb = sub.add_parser("heartbeat")
    hb.add_argument("session")
    f = sub.add_parser("finish", help="End a registration and release its resources once its work has stopped")
    f.add_argument("session", nargs="?")
    f.add_argument("--safe", action="store_true", required=True)
    f.add_argument("--failed", action="store_true")
    f.add_argument("--status", default="Finished")
    for verb in ["pause", "resume", "update"]:
        c = sub.add_parser(verb, help={"pause": "Ask a session to reach a stopping point", "resume": "Resume and reacquire resources", "update": "Request a short status update"}[verb])
        c.add_argument("session")
        c.add_argument("--reason", default="Requested from Arbiter CLI")
        c.add_argument("--timeout", type=float, default=60)
    ib = sub.add_parser("inbox", help="Pending requests and messages for a session")
    session_option(ib)
    rp = sub.add_parser("report", help="Report what a session is doing; answer a status request")
    rp.add_argument("status")
    session_option(rp)
    rp.add_argument("--command", dest="command_id", help="ID of the status request this answers")
    rp.add_argument("--stopping-point")
    rp.add_argument("--progress")
    rp.add_argument("--checkpoint", help="Last complete application checkpoint")
    rp.add_argument("--blocker")
    m = sub.add_parser("message", help="Post a message to a session or the shared board")
    m.add_argument("text")
    session_option(m)
    m.add_argument("--to")
    m.add_argument("--topic", default="general")
    m.add_argument("--reply-to")
    a = sub.add_parser("ack", help="Acknowledge a message")
    a.add_argument("message")
    session_option(a)
    a = sub.add_parser("answer", help="Answer a provider approval or input request with its exact JSON response")
    a.add_argument("approval")
    a.add_argument("response", help='Provider response JSON, e.g. {"decision":"decline"}')
    sub.add_parser("approvals", help="List pending provider requests with their messages and payloads")
    for verb in ("approve", "decline"):
        a = sub.add_parser(verb, help=f"{verb.capitalize()} one provider action or confirmation")
        a.add_argument("approval")
    rs = sub.add_parser("reserve", help="Request a session's resource bundle (manual integrations)")
    session_option(rs)
    resources(rs, positional=True)
    rs.add_argument("--eta", help="Expected duration of this phase, e.g. 5m")
    rs.add_argument("--wait", action="store_true")
    promote = sub.add_parser("promote", help="Move a waiting reservation to the front of the queue")
    promote.add_argument("reservation", help="Reservation ID shown by arbiter status")
    ck = sub.add_parser("checkpoint", help="Acknowledge a pause: resource-using work has stopped")
    ck.add_argument("status")
    session_option(ck)
    ck.add_argument("--safe", required=True, action="store_true")
    ck.add_argument("--wait", action="store_true", help="Block until resumed and resources are reacquired")
    hk = sub.add_parser("hook", help="Lifecycle hook for Claude Code or Codex (reads the event on stdin)")
    hk.add_argument("provider", choices=["claude", "codex"])
    mcp = sub.add_parser("mcp", help="Expose Arbiter tools over MCP stdio")
    mcp.add_argument("--name", default="external-session")
    agent = sub.add_parser("agent", help="Launch a managed Codex or Claude session")
    agent.add_argument("provider", choices=["codex", "claude"])
    agent.add_argument("prompt")
    agent.add_argument("--name")
    agent.add_argument("--cwd", default=os.getcwd())
    agent.add_argument("--once", action="store_true", help="Exit after first completed turn instead of waiting for messages")
    agent.add_argument("--model")
    agent.add_argument("--resume", help="Provider conversation ID to resume (not an Arbiter session ID)")
    demo = sub.add_parser("demo", help="Run a model-free contention, messaging and pause/resume demonstration")
    demo.add_argument("--delay", type=float, default=.4)
    return p


def split_command(argv):
    """`arbiter run [options] [RESOURCE ...] -- COMMAND ...`: everything after the first -- is the command."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command_argv = []
    if argv and argv[0] == "run":
        argv, command_argv = split_command(argv)
    args = parser().parse_args(argv)
    try:
        return dispatch(args, command_argv)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"arbiter: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def dispatch(args, command_argv):
    from . import identity, workers
    command = args.command
    if command == "serve":
        from .server import serve
        return serve(args)
    if command == "hook":
        from .hook import main as hook
        return hook(args.provider)
    if command in {"install", "uninstall"}:
        from . import install
        return install.install(args) if command == "install" else install.uninstall(args)
    if command == "mcp":
        from .mcp import run
        return run(args.name)
    if command == "agent":
        from .adapters import run_agent
        return run_agent(args)
    if command == "run":
        return workers.run_job(args, command_argv)
    if command == "hold":
        return workers.hold(args)
    if command == "release":
        return workers.release(args)
    if command == "extend":
        return workers.extend(args)
    if command == "wait":
        return workers.wait(args)
    if command == "disk":
        return {"claim": workers.claim_disk, "check": workers.check_disk, "release": workers.release_disk}[args.disk_command](args)
    if command == "demo":
        return workers.demo(args.delay)
    client = Client()
    if command == "open":
        import webbrowser
        link = client.request("/api/launch", {})["url"]
        print(link)
        webbrowser.open(link)
        return
    if command is None or command == "status":
        state = client.snapshot()
        if command == "status" and args.json:
            return output(state)
        print(workers.status_text(state))
        if command is None:
            from .client import board_location
            print(board_location(client.url))
        return
    if command in {"recover", "rerun-command", "resume-conversation"}:
        return output(client.action("recover", session=args.session, confirm_unknown=args.confirm_unknown, recovery_action=command))
    if command == "cancel-retry":
        return output(client.action("cancel_retry", session=args.session))
    if command == "promote":
        result = client.action("promote", reservation=args.reservation)
        return print(f"{result['id']} is {'running' if result['state'] == 'granted' else 'first in the queue'}")
    if command == "whoami":
        sid = identity.current(client)
        if not sid:
            raise ValueError("not running inside a Claude Code, Codex or Arbiter-managed session")
        s = client.snapshot()["sessions"][sid]
        return print(f"{s['name']} {sid} ({s['kind']}, {s['state']})")
    if command == "approvals":
        return output([a for a in client.snapshot()["approvals"].values() if a["state"] == "pending"])
    if command in {"answer", "approve", "decline"}:
        a = client.snapshot()["approvals"].get(args.approval)
        if not a:
            raise ValueError("unknown approval")
        if command == "answer":
            response = json.loads(args.response)
        else:
            from .approvals import confirmation_response
            response = confirmation_response(a, command == "approve")
        return output(client.action("approval_answer", session=a["session"], approval=args.approval, response=response))
    if command == "events":
        return output(client.request(f"/api/events?after={args.after}"))
    if command == "register":
        data = {"name": args.name, "kind": args.kind, "cwd": os.getcwd()}
        if args.pid:
            data["pid"] = args.pid
        result = client.action("register", **data)
        result.pop("token", None)
        return output(result)
    if command in {"pause", "resume", "update"}:
        return output(client.action("control", session=args.session, action="status" if command == "update" else command, reason=args.reason, timeout=args.timeout))
    if command == "heartbeat":
        return output(client.action("heartbeat", session=args.session))
    sid = identity.require(client, getattr(args, "session", None))
    if command == "inbox":
        return output(client.inbox(sid))
    data = {"session": sid}
    if command == "reserve":
        if args.eta:
            data["eta"] = workers.duration(args.eta)
        data.update(cpu=args.cpu, memory_mb=args.memory_mb, exclusive=args.resources + args.exclusive, priority=args.priority)
    if command == "report":
        data.update(status=args.status, command=args.command_id, stopping_point=args.stopping_point, progress=args.progress, blocker=args.blocker, checkpoint=args.checkpoint)
    if command == "ack":
        data["message"] = args.message
    if command in {"checkpoint", "finish"}:
        data.update(status=args.status, safe=args.safe)
    if command == "finish":
        data["failed"] = args.failed
    if command == "message":
        data.update(text=args.text, to=args.to, topic=args.topic, reply_to=args.reply_to)
    result = client.action("ack_message" if command == "ack" else command, **data)
    if getattr(args, "wait", False):
        state = client.wait(sid, result["id"] if command == "reserve" else None)
        if command == "reserve":
            result = state["reservations"][result["id"]]
            result["session_state"] = state["sessions"][sid]["state"]
    output(result)
