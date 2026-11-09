import argparse
import json
import os
import sys

from .client import Client


def output(value):
    print(json.dumps(value, indent=2))


def parser():
    p = argparse.ArgumentParser(prog="arbiter", description="Local resource scheduler and message board")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="Run the localhost daemon")
    s.add_argument("--port", type=int, default=7483)
    s.add_argument("--unix-socket", help="Serve local HTTP over a Unix socket instead of TCP (CLI/MCP only)")
    s.add_argument("--cpu", type=float, help="Concurrent CPU scheduling budget (default: 80%% of logical CPUs)")
    s.add_argument("--memory-mb", type=int, help="Job memory budget in MiB (default: 70%% of RAM)")
    s.add_argument("--headroom-mb", type=int, default=512, help="Keep this much measured free memory (default: 512 MiB)")
    sub.add_parser("status", help="Print sessions, reservations and capacity")
    e = sub.add_parser("events", help="Read durable message-board events")
    e.add_argument("--after", type=int, default=0)
    r = sub.add_parser("register", help="Register an independently managed session")
    r.add_argument("name")
    r.add_argument("--kind", default="external", choices=["external", "codex", "claude", "demo"])
    h = sub.add_parser("heartbeat")
    h.add_argument("session")
    f = sub.add_parser("finish", help="Release ownership after confirming all child work stopped")
    f.add_argument("session")
    f.add_argument("--safe", action="store_true", required=True)
    f.add_argument("--failed", action="store_true")
    f.add_argument("--status", default="Finished")
    for verb in ["pause", "resume", "update"]:
        c = sub.add_parser(verb, help={"pause": "Ask a session to reach a stopping point", "resume": "Resume and reacquire resources", "update": "Request a short status update"}[verb])
        c.add_argument("session")
        c.add_argument("--reason", default="Requested from Arbiter CLI")
        c.add_argument("--timeout", type=float, default=60)
    i = sub.add_parser("inbox")
    i.add_argument("session")
    r = sub.add_parser("report")
    r.add_argument("session")
    r.add_argument("status")
    r.add_argument("--command-id")
    r.add_argument("--stopping-point")
    m = sub.add_parser("message")
    m.add_argument("session", help="Sending session ID")
    m.add_argument("text")
    m.add_argument("--to")
    m.add_argument("--topic", default="general")
    m.add_argument("--reply-to")
    a = sub.add_parser("ack")
    a.add_argument("session")
    a.add_argument("message")
    a = sub.add_parser("answer", help="Answer a provider approval or input request with its exact JSON response")
    a.add_argument("approval")
    a.add_argument("response", help='Provider response JSON, e.g. {"decision":"decline"}')
    sub.add_parser("approvals", help="List pending provider requests with their messages and payloads")
    for verb in ("approve", "decline"):
        a = sub.add_parser(verb, help=f"{verb.capitalize()} one provider action or confirmation")
        a.add_argument("approval")
    r = sub.add_parser("reserve")
    r.add_argument("session")
    resources(r)
    r.add_argument("--wait", action="store_true")
    rel = sub.add_parser("release")
    rel.add_argument("session")
    rel.add_argument("reservation")
    ck = sub.add_parser("checkpoint")
    ck.add_argument("session")
    ck.add_argument("status")
    ck.add_argument("--safe", required=True, action="store_true")
    ck.add_argument("--wait", action="store_true")
    mcp = sub.add_parser("mcp", help="Expose Arbiter tools over MCP stdio")
    mcp.add_argument("--name", default="external-session")
    run = sub.add_parser("run", help="Schedule and supervise a command; pause drains the current command")
    run.add_argument("--name", default="job")
    resources(run)
    run.add_argument("argv", nargs=argparse.REMAINDER)
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


def resources(p):
    p.add_argument("--cpu", type=float, default=0)
    p.add_argument("--memory-mb", type=float, default=0)
    p.add_argument("--exclusive", action="append", default=[])
    p.add_argument("--priority", type=float, default=0)


def main():
    args = parser().parse_args()
    try:
        command = args.command
        if command == "serve":
            from .server import serve
            return serve(args)
        if command == "mcp":
            from .mcp import run
            return run(args.name)
        if command == "agent":
            from .adapters import run_agent
            return run_agent(args)
        if command == "run":
            from .workers import run_job
            return run_job(args)
        if command == "demo":
            from .workers import demo
            return demo(args.delay)
        client = Client()
        if command == "status":
            return output(client.snapshot())
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
            result = client.action("register", name=args.name, kind=args.kind, cwd=os.getcwd())
            result.pop("token", None)
            return output(result)
        if command in {"pause", "resume", "update"}:
            return output(client.action("control", session=args.session, action="status" if command == "update" else command, reason=args.reason, timeout=args.timeout))
        if command == "inbox":
            return output(client.inbox(args.session))
        data = {"session": args.session}
        if command == "reserve":
            data.update(cpu=args.cpu, memory_mb=args.memory_mb, exclusive=args.exclusive, priority=args.priority)
        if command == "report":
            data.update(status=args.status, command=args.command_id, stopping_point=args.stopping_point)
        if command in {"release", "ack"}:
            data.update({"reservation" if command == "release" else "message": getattr(args, "reservation" if command == "release" else "message")})
        if command in {"checkpoint", "finish"}:
            data.update(status=args.status, safe=args.safe)
        if command == "finish":
            data["failed"] = args.failed
        if command == "message":
            data.update(text=args.text, to=args.to, topic=args.topic, reply_to=args.reply_to)
        result = client.action("ack_message" if command == "ack" else command, **data)
        if getattr(args, "wait", False):
            state = client.wait(args.session, result["id"] if command == "reserve" else None)
            if command == "reserve":
                result = state["reservations"][result["id"]]
                result["session_state"] = state["sessions"][args.session]["state"]
        output(result)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"arbiter: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
