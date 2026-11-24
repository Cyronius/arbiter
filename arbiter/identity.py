"""Which Arbiter session is the caller? Managed child, existing Claude Code/Codex conversation, or none."""
from __future__ import annotations

import os

from . import system


def provider(env=None) -> tuple[str, str] | None:
    env = os.environ if env is None else env
    if env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude", env["CLAUDE_CODE_SESSION_ID"]
    if env.get("CODEX_THREAD_ID"):
        return "codex", env["CODEX_THREAD_ID"]
    return None


def attach_data(kind: str, external_id: str, cwd: str | None = None, env=None) -> dict:
    return {"provider": kind, "external_id": external_id, "cwd": cwd or os.getcwd(), "pid": system.agent_pid(env)}


def current(client, env=None) -> str | None:
    """The caller's session ID, attaching an existing conversation on first use. None outside any agent."""
    env = os.environ if env is None else env
    if env.get("ARBITER_SESSION"):
        return env["ARBITER_SESSION"]
    found = provider(env)
    if not found:
        return None
    return client.action("attach", **attach_data(*found, env=env))["id"]


def require(client, given: str | None = None) -> str:
    sid = given or current(client)
    if not sid:
        raise ValueError("no session: pass --session, or run this from a Claude Code or Codex session")
    return sid
