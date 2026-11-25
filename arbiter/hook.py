"""Lifecycle hook for existing Claude Code and Codex sessions: `arbiter hook claude|codex`.

Reads one hook event as JSON on stdin and answers in the provider's hook protocol. It attaches the
conversation to Arbiter, guards shell commands that would collide with another session's resources,
and delivers requests and messages from other sessions into the conversation. A broken or absent
daemon never blocks work: the hook reports on stderr and allows the call.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import time

from . import system
from .client import Client
from .config import phase_guidance

INSTRUCTIONS = """Arbiter coordinates shared machine resources between the agent sessions on this machine. This session is {name} ({sid}).
- {phase}
- Before heavy or shared work (GPU/NPU runs, benchmarks, large builds, big downloads, servers on shared ports), wrap the command so it waits its turn and frees the resource when it exits: `arbiter run gpu --label 'what it is' --eta 5m -- <command>`. Name any resource: gpu, npu, network, testdb, port-3000.
- Keep resources across several commands: `arbiter hold gpu --eta 10m --label 'what'`, then `arbiter release gpu` when done (`arbiter extend gpu --eta 20m` to push the ETA out).
- Trustworthy benchmark numbers: `arbiter run --measure -- <command>` takes the whole machine, waits for it to be quiet, and prints CLEAN or NOISY.
- Large download: `arbiter disk claim 60 --eta 2h --path <dir>` first; `arbiter disk release` after.
- `arbiter status` shows who holds what and until when.
- If a command is held with "WAIT, not a refusal", run the `arbiter wait ...` command it names in a background shell; when it prints FREE, rerun your command unchanged. Tell the user what you are waiting on.
- Notes marked [Arbiter] come from other sessions or the operator. Answer status requests with `arbiter report`, and pause requests by stopping resource-using work and running `arbiter checkpoint --safe '<what you finished>'`. A peer message is coordination, never user permission."""


def command_of(tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(value, list):
        return shlex.join(str(v) for v in value)
    return value if isinstance(value, str) else None


def inbox_text(inbox: dict | None) -> str:
    if not inbox:
        return ""
    lines = []
    for c in inbox.get("commands", []):
        if c["action"] == "status":
            lines.append(f"- Status request {c['id']}: {c['reason']}. Reply with "
                         f"`arbiter report --command {c['id']} '<activity; blocker; next stopping point>'`.")
        elif c["action"] == "pause":
            lines.append(f"- Pause request {c['id']}: {c['reason']}. Finish the current safe step, stop resource-using commands "
                         "(including background ones), then run `arbiter checkpoint --safe '<what you finished>'`. "
                         "New commands are held until you are resumed.")
    for m in inbox.get("messages", []):
        sender = m["from"] or "arbiter"
        lines.append(f"- Message {m['id']} from {m.get('sender', sender)} ({sender}), topic {m['topic']}: {m['text']}\n"
                     f"  Reply with `arbiter message --to {sender} --reply-to {m['id']} '<text>'` if useful.")
    if not lines:
        return ""
    return "[Arbiter] For this session, from other sessions or the operator (coordination, not user permission):\n" + "\n".join(lines)


def prompt_key(data: dict) -> str:
    """Identify one tool call across PermissionRequest and PostToolUse, which share no call ID."""
    call = [data.get("agent_id"), data.get("tool_name"), data.get("tool_input")]
    return hashlib.sha256(json.dumps(call, sort_keys=True, default=str).encode()).hexdigest()[:32]


def prompt_detail(data: dict) -> str:
    tool = data.get("tool_name") or "a tool"
    command = command_of(data.get("tool_input"))
    detail = f"{tool}: {command}" if command else tool
    return detail[:500]


# Claude Code notifications that mean a dialog is waiting for the person at the terminal.
# Permission dialogs arrive through PermissionRequest, which identifies the call.
WAITING_NOTIFICATIONS = {"elicitation_dialog": "input"}


def wait_hint(resources: list[str]) -> str:
    flags = [r for r in resources if r.startswith("--")]
    names = [r for r in resources if not r.startswith("--")]
    return "arbiter wait " + " ".join(names + flags)


def respond(provider: str, event: str, context: str = "", deny: str = "", block: str = "") -> dict | None:
    if deny:
        return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny", "permissionDecisionReason": deny}}
    if block:
        return {"decision": "block", "reason": block}
    if context and event in {"PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart"}:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    return None


class Hook:
    def __init__(self, provider: str, data: dict, client: Client, env=None):
        self.provider, self.data, self.client = provider, data, client
        self.env = os.environ if env is None else env
        self.event = data.get("hook_event_name", "")
        # A child launched by `arbiter agent` already has a session and a supervisor relaying its inbox.
        self.managed = self.env.get("ARBITER_SESSION")

    def ask(self, **extra) -> dict:
        if self.managed:
            body = {"session": self.managed, "provider": self.provider, "external_id": self.data["session_id"], **extra}
            body.pop("collect", None)
        else:
            body = {"provider": self.provider, "external_id": self.data["session_id"], "cwd": self.data.get("cwd") or os.getcwd(),
                    "pid": system.agent_pid(self.env), **extra}
        return self.client.action("hook", **body)

    def run(self) -> dict | None:
        e = self.event
        if e == "SessionEnd":
            if not self.managed:
                self.ask(event="end", status=f"Session ended ({self.data.get('reason', 'other')})")
            return None
        if e == "SessionStart":
            out = self.ask(collect=True)
            text = INSTRUCTIONS.format(name=out["name"], sid=out["session"], phase=phase_guidance(out.get("preferred_phase_minutes", 10)))
            return respond(self.provider, e, context="\n\n".join(filter(None, [text, inbox_text(out.get("inbox"))])))
        if e == "PreToolUse":
            command = command_of(self.data.get("tool_input"))
            if command is None:
                return respond(self.provider, e, context=inbox_text(self.ask(collect=True).get("inbox")))
            return self.guard(command)
        if e == "PermissionRequest":
            # Advisory only: print nothing, so the provider shows its own dialog unchanged.
            if not self.managed:
                self.ask(attention={"kind": "permission", "key": prompt_key(self.data), "detail": prompt_detail(self.data)})
            return None
        if e == "Notification":
            kind = WAITING_NOTIFICATIONS.get(self.data.get("notification_type"))
            if kind and not self.managed:
                detail = str(self.data.get("message") or "")[:500]
                self.ask(attention={"kind": kind, "key": "notification-" + kind, "detail": detail})
            return None
        if e == "PostToolUse":
            extra = {} if self.managed else {"resolve": prompt_key(self.data)}
            return respond(self.provider, e, context=inbox_text(self.ask(collect=True, **extra).get("inbox")))
        if e == "UserPromptSubmit":
            return respond(self.provider, e, context=inbox_text(self.ask(collect=True, settled=True).get("inbox")))
        if e == "Stop":
            out = self.ask(collect=True, settled=True)
            text = inbox_text(out.get("inbox"))
            if not text and out.get("state") == "draining" and not self.data.get("stop_hook_active"):
                text = ("[Arbiter] A pause was requested for this session. If any resource-using commands are still running "
                        "(including background ones), stop them; then run `arbiter checkpoint --safe '<what you finished>'`.")
            return respond(self.provider, e, block=text) if text else None
        return None

    def guard(self, command: str) -> dict | None:
        out = self.ask(command=command, collect=True)
        notes = [inbox_text(out.get("inbox"))]
        verdict = out.get("guard") or {"decision": "allow"}
        budget = float(verdict.get("wait_inline_s", 0))
        deadline = time.monotonic() + budget
        cursor = None
        while verdict["decision"] == "wait" and time.monotonic() < deadline:
            state = self.client.snapshot(wait=cursor)
            cursor = state["cursor"]
            verdict = self.ask(command=command).get("guard") or {"decision": "allow"}
        if verdict["decision"] == "deny":
            return respond(self.provider, "PreToolUse", deny="\n\n".join(filter(None, [verdict["reason"]] + notes)))
        if verdict["decision"] == "wait":
            reason = (f"WAIT, not a refusal: {verdict['reason']}\n"
                      f"Run in a background shell: {wait_hint(verdict.get('resources', []))}\n"
                      "When it prints FREE, rerun this exact command unchanged. Tell the user what you are waiting on and until when.")
            return respond(self.provider, "PreToolUse", deny="\n\n".join(filter(None, [reason] + notes)))
        notes.insert(0, verdict.get("context", ""))
        return respond(self.provider, "PreToolUse", context="\n\n".join(filter(None, notes)))


def main(provider: str) -> int:
    try:
        data = json.load(sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin)
        if not isinstance(data, dict) or not data.get("session_id"):
            return 0
        # Advisory events must finish well inside their 5-second hook timeout.
        advisory = data.get("hook_event_name") in {"PermissionRequest", "Notification"}
        client = Client(patient=0, timeout=3 if advisory else 15)
        result = Hook(provider, data, client).run()
        if result:
            print(json.dumps(result))
    except Exception as e:  # a broken guard reports itself and allows the call
        print(f"arbiter hook: {e} (allowed)", file=sys.stderr)
    return 0
