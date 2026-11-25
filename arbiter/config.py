"""Optional TOML configuration: named resources, the session guard and measurement thresholds."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

DEFAULTS = {
    "scheduler": {"preferred_phase_minutes": 10, "auto_approve_inbox": False, "preserve_interactivity": False},
    "guard": {"enabled": True, "wait_inline_s": 240, "pressure": True, "cpu_busy_pct": 95, "read_only": []},
    "measure": {"noise_core_pct": 50, "quiet_pct": 15, "quiet_seconds": 3, "quiet_timeout_s": 120},
    "resource": {},
    "operator": {"provider": "", "model": "", "command": [], "instructions": "",
                 "approve_inbox": False, "manage_resources": False, "reply_messages": False, "review_status": False},
}

STARTER = """\
# Arbiter configuration. Resources need no entry to be used; add one to share a
# resource between several holders (capacity) or to let the session guard catch
# commands that use it without asking (match: a Python regular expression).

[resource.gpu]
match = '\\b(?:ollama|llama-server|llama-cli|llama-bench|vllm)\\b'

[resource.npu]

[resource.network]
capacity = 2
match = '\\b(?:hf download|huggingface-cli download|docker pull|aws s3 cp|wget)\\b'

[resource.build]
match = '\\b(?:cargo build|cmake --build|ninja|docker build)\\b'

[guard]
# Hold a guarded command this long inside the hook before telling the agent to wait.
wait_inline_s = 240
# Hold new commands while measured free memory is below headroom or CPU is this busy.
pressure = true
cpu_busy_pct = 95

[measure]
# A process outside a --measure run using more than this % of one core makes it NOISY.
noise_core_pct = 50
"""


def path() -> Path:
    if os.environ.get("ARBITER_CONFIG"):
        return Path(os.environ["ARBITER_CONFIG"])
    if os.name == "nt":
        return app_directory() / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "arbiter" / "config.toml"


def app_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Arbiter"


def merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate_operator(operator: dict) -> None:
    if not isinstance(operator, dict):
        raise ValueError("operator must be a table")
    if operator["provider"] not in ("", "codex", "claude", "pi", "command"):
        raise ValueError("operator.provider must be codex, claude, pi or command")
    if not isinstance(operator["model"], str):
        raise ValueError("operator.model must be a string")
    if len(operator["model"]) > 250 or operator["model"].startswith("-") or (operator["model"] and not operator["model"].isprintable()):
        raise ValueError("Enter a valid model identifier of up to 250 characters")
    if not isinstance(operator["instructions"], str) or len(operator["instructions"]) > 4000:
        raise ValueError("Operator instructions must be text of up to 4000 characters")
    command = operator["command"]
    if not isinstance(command, list) or any(not isinstance(arg, str) or not arg for arg in command):
        raise ValueError("operator.command must be an array of nonempty strings")
    if operator["provider"] == "command" and not command:
        raise ValueError("operator.command is required for the command provider")
    if any(type(operator[key]) is not bool for key in ("approve_inbox", "manage_resources", "reply_messages", "review_status")):
        raise ValueError("operator policy settings must be boolean")


def validate(cfg: dict) -> dict:
    validate_operator(cfg["operator"])
    validate_phase_minutes(cfg["scheduler"].get("preferred_phase_minutes") if isinstance(cfg["scheduler"], dict) else None)
    if any(type(cfg["scheduler"][key]) is not bool for key in ("auto_approve_inbox", "preserve_interactivity")):
        raise ValueError("Scheduler switches must be boolean")
    for name, r in cfg["resource"].items():
        if not isinstance(r, dict):
            raise ValueError(f"resource.{name} must be a table")
        cap = r.get("capacity", 1)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError(f"resource.{name}.capacity must be a positive integer")
        if "match" in r:
            re.compile(r["match"])
    return cfg


def validate_phase_minutes(value) -> None:
    if type(value) is not int or not 1 <= value <= 1440:
        raise ValueError("Preferred reservation length must be 1–1440 whole minutes")


def phase_guidance(minutes: int) -> str:
    return (f"Plan reservations around {minutes}-minute phases when you can split the work. "
            "Release at a safe checkpoint and queue the next phase separately. "
            "Set a different ETA when the work needs it.")


def load(file: Path | None = None) -> dict:
    file = file or path()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        data = {}
    return validate(merge(DEFAULTS, data))


class Config:
    """Reloads the file when it changes; keeps the last good version if an edit is invalid."""

    def __init__(self, file: Path | None = None, data: dict | None = None):
        self.file = file
        self.stamp = None
        self.data = validate(merge(DEFAULTS, data)) if data is not None else DEFAULTS
        self.error = None
        self.fixed = data is not None
        self.patterns: dict[str, re.Pattern] = {}
        self.compile()
        if not self.fixed:
            self.refresh()

    def compile(self):
        self.patterns = {n: re.compile(r["match"]) for n, r in self.data["resource"].items() if r.get("match")}

    def refresh(self):
        if self.fixed:
            return
        file = self.file or path()
        try:
            stamp = file.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return
        self.stamp = stamp
        try:
            self.data = load(file)
            self.error = None
            self.compile()
        except (ValueError, tomllib.TOMLDecodeError, re.error) as e:
            self.error = f"{file}: {e}"

    def capacity(self, resource: str) -> int:
        return self.data["resource"].get(resource, {}).get("capacity", 1)

    def __getitem__(self, key):
        return self.data[key]
