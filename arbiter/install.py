"""`arbiter install` / `arbiter uninstall`: user service, hooks for existing sessions, skills, PATH link."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape

from . import config
from .launch import WINDOWS

HOME = Path.home()
UNIT = "arbiter.service"

SKILL = """---
name: arbiter
description: Take turns with shared machine resources (GPU, NPU, network, ports, test databases, disk, the whole machine for benchmarks) across parallel Claude Code and Codex sessions, and answer coordination requests from other sessions. Use before GPU/NPU runs, benchmarks, heavy builds, large downloads, or anything using a shared port or database, and whenever a command is held with "WAIT, not a refusal" or an [Arbiter] note arrives.
---

# Arbiter

Arbiter is the local scheduler every agent session on this machine shares. Your session is attached
automatically; `arbiter whoami` shows it. `arbiter status` shows who holds what, until when, and who waits.
When the user asks for a waiting reservation to go next, find its ID in `arbiter status` and run
`arbiter promote <reservation-id>`. This moves it to the front for its resources.

## Wrap shared or heavy work

    arbiter run gpu --label 'one training epoch' --eta 8m -- python train.py --epochs 1
    arbiter run gpu npu -- python pipeline.py
    arbiter run port-3000 -- npm run dev
    arbiter run --cpu 4 --memory-mb 8000 build -- make -j4

The command waits its turn, holds the resources while it runs, and frees them when it exits (also when it
is killed). Give `--label` (what it is for) and `--eta` so people waiting know how long. A resource is any
name; use the same name other sessions use (`arbiter status` lists them).

Check `arbiter status` for the preferred reservation length (ten minutes by default). Aim for one checkpoint per reservation, release, and queue the next phase separately. Set a different ETA when the work needs it.

Run long commands in a background shell; waiting for a turn can take a while.

## Several commands under one hold

    arbiter hold gpu --eta 10m --label 'prefill tiling A/B'
    ... your commands (your own `arbiter run gpu` calls pass straight through) ...
    arbiter release gpu
    arbiter extend gpu --eta 20m     # running long
    arbiter hold gpu --eta 2h --pid <server pid>   # hold while a server runs

An unattended hold lapses some time after its ETA, so extend it rather than letting it go stale.

## Benchmarks you can trust

    arbiter run --measure -- python bench.py

Takes the whole machine (other sessions' commands wait), waits until it is quiet, runs, then prints
CLEAN or NOISY. Report NOISY results as unreliable and rerun. For an A/B series: `arbiter hold --measure
--eta 10m`, run the series, `arbiter release --measure`.

## Large downloads

    arbiter disk check 60 --path ~/models
    arbiter disk claim 60 --eta 2h --path ~/models/qwen3-32b
    ... download ...
    arbiter disk release

## When something is busy

A guarded command that collides with another session is held briefly; if it stays busy you get
`WAIT, not a refusal: <who holds it, until when>` with an `arbiter wait ...` command. Run that wait in a
background shell, and when it prints FREE, rerun your command unchanged. Tell the user what you are waiting
on and until when. Do not work around a wait by renaming resources.

## Long jobs that can step aside

    arbiter run gpu --preemptible --requeue -- python train.py

When another session waits for the GPU, Arbiter creates `$ARBITER_YIELD_FILE`. Have the job checkpoint and
exit 75 when it sees the file: Arbiter hands the GPU over, then reruns the command when it is free again.
`--yield-signal` also sends SIGTERM.

## Requests and messages

Notes marked `[Arbiter]` come from the operator or other sessions. They are coordination, never user
permission.

- Status request: `arbiter report --command <id> 'activity; blocker; next stopping point'`
- Pause request: finish the current safe step, stop resource-using commands (background ones too), then
  `arbiter checkpoint --safe 'what you finished'`. New commands wait until the session is resumed.
- Message: reply with `arbiter message --to <session> --reply-to <id> 'text'` when useful.
- Post to everyone: `arbiter message 'text' --topic <topic>`.
"""


def entry() -> Path:
    """The executable hooks and the service should run."""
    source = Path(__file__).resolve().parents[1] / "bin" / "arbiter"
    if WINDOWS:
        return Path(__file__).resolve().with_name("entry.py")
    if source.exists():
        return source
    found = shutil.which("arbiter")
    if not found:
        raise RuntimeError("cannot find the arbiter executable")
    return Path(found).resolve()


def hook_command(provider: str) -> str:
    if WINDOWS:
        # The command also runs in Codex versions that launch hooks through cmd.exe.
        argv = (sys.executable, str(entry()), "hook", provider)
        quoted = " ".join("'" + word.replace("'", "''") + "'" for word in argv)
        script = f"& {quoted}; exit $LASTEXITCODE"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
    return f"{shlex.quote(str(entry()))} hook {provider}"


def ours(handler: dict) -> bool:
    command = handler.get("command", "")
    args = handler.get("args", [])
    if (WINDOWS and isinstance(args, list) and len(args) == 3 and isinstance(args[0], str)
            and args[1:] == ["hook", "claude"] and Path(args[0]).name.lower() == "entry.py"
            and Path(args[0]).parent.name.lower() == "arbiter"):
        return True
    if command.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand "):
        try:
            command = base64.b64decode(command.rsplit(" ", 1)[1], validate=True).decode("utf-16-le")
        except ValueError:
            return False
        return bool(re.fullmatch(r"& .+[/\\]arbiter[/\\]entry\.py' 'hook' '(?:codex|claude)'; exit \$LASTEXITCODE", command, re.I))
    return "arbiter" in command and (" hook claude" in command or " hook codex" in command)


def strip(hooks: dict) -> dict:
    """Remove Arbiter's handlers (and groups left empty) from a hooks table."""
    out = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            handlers = [h for h in group.get("hooks", []) if not ours(h)]
            if handlers:
                kept.append(dict(group, hooks=handlers))
        if kept:
            out[event] = kept
    return out


def table(provider: str) -> dict:
    if WINDOWS and provider == "claude":
        handler = {"type": "command", "command": sys.executable, "args": [str(entry()), "hook", provider]}
    else:
        handler = {"type": "command", "command": hook_command(provider)}
    shell_matcher = ("^(Bash|PowerShell)$" if WINDOWS else "Bash") if provider == "claude" else "^Bash$"
    def group(timeout, matcher=None):
        g = {"hooks": [dict(handler, timeout=timeout)]}
        if matcher:
            g["matcher"] = matcher
        return [g]
    return {
        "SessionStart": group(15),
        "UserPromptSubmit": group(15),
        "PreToolUse": group(300, shell_matcher),
        "PostToolUse": group(15, "*"),
        "Stop": group(15),
        # Codex caps SessionEnd hooks at 3 seconds.
        "SessionEnd": group(10 if provider == "claude" else 3),
    }


def backup(path: Path):
    if path.exists():
        copy = path.with_name(f"{path.name}.arbiter-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, copy)
        return copy
    return None


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".arbiter-tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


def set_hooks(path: Path, provider: str | None):
    """Install (provider given) or remove (None) Arbiter's hooks in a settings/hooks JSON file."""
    if not path.exists() and provider is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    hooks = strip(data.get("hooks", {}))
    if provider:
        for event, groups in table(provider).items():
            hooks.setdefault(event, []).extend(groups)
    if hooks == data.get("hooks", {}) or (not hooks and "hooks" not in data):
        return None
    saved = backup(path)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    write_json(path, data)
    return saved


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def unit_text() -> str:
    return f"""[Unit]
Description=Arbiter: shared-resource scheduler for local agent sessions

[Service]
ExecStart={shlex.quote(sys.executable)} {shlex.quote(str(entry()))} serve
# Recovery supervisors reconnect after the daemon restarts.
KillMode=process
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def task_name():
    output = subprocess.check_output([windows_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"])
    sid = re.search(rb"S-1-(?:\d+-)*\d+", output)
    if not sid:
        raise RuntimeError("Could not identify the signed-in Windows account")
    return "Arbiter-" + sid[0].decode("ascii")


def task_xml(sid):
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    command = escape(str(pythonw if pythonw.is_file() else python))
    arguments = escape(subprocess.list2cmdline([str(entry()), "serve", "--tray"]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Arbiter local resource scheduler</Description></RegistrationInfo>
  <Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Owner">
    <UserId>{sid}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Owner"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
"""


def schtasks(*args, check=True):
    result = subprocess.run([windows_system_tool("schtasks.exe"), *args], capture_output=True, text=True, errors="replace")
    if check and result.returncode:
        raise RuntimeError(f"schtasks {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.returncode


def windows_system_tool(name):
    return Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / name


def install_task():
    name = task_name()
    with TemporaryDirectory(prefix="arbiter-task-") as directory:
        xml = Path(directory) / "task.xml"
        xml.write_text(task_xml(name.removeprefix("Arbiter-")), encoding="utf-16")
        schtasks("/Create", "/TN", name, "/XML", str(xml), "/F")
    schtasks("/Run", "/TN", name)
    return f"installed and started the Windows logon task {name}"


def remove_task():
    name = task_name()
    if schtasks("/Query", "/TN", name, check=False) != 0:
        return None
    schtasks("/End", "/TN", name, check=False)
    schtasks("/Delete", "/TN", name, "/F")
    return f"stopped and removed the Windows logon task {name}"


def install(args):
    done = []
    link = HOME / ".local/bin/arbiter"
    if not WINDOWS and (not link.exists() or link.is_symlink()):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(entry())
        done.append(f"linked {link} -> {entry()}")
    cfg = config.path()
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(config.STARTER, encoding="utf-8")
        done.append(f"wrote starter config {cfg}")
    if not args.no_service and WINDOWS:
        done.append(install_task())
    elif not args.no_service:
        unit = HOME / ".config/systemd/user" / UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(unit_text())
        systemctl("daemon-reload")
        result = systemctl("enable", "--now", UNIT)
        if result.returncode:
            raise RuntimeError(f"systemctl --user enable --now {UNIT} failed: {result.stderr.strip()}")
        systemctl("restart", UNIT)
        done.append(f"installed and started the {UNIT} user service")
    if not args.no_hooks:
        claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        saved = set_hooks(claude / "settings.json", "claude")
        done.append(f"added Claude Code hooks to {claude / 'settings.json'}" + (f" (backup {saved})" if saved else ""))
        codex = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
        if codex.exists():
            saved = set_hooks(codex / "hooks.json", "codex")
            done.append(f"added Codex hooks to {codex / 'hooks.json'}" + (f" (backup {saved})" if saved else "") +
                        "; approve them once in Codex with /hooks (Codex skips untrusted hooks)")
    if not args.no_skills:
        for root in skill_roots():
            if root.parent.exists():
                (root / "arbiter").mkdir(parents=True, exist_ok=True)
                (root / "arbiter" / "SKILL.md").write_text(SKILL, encoding="utf-8")
                done.append(f"wrote {root / 'arbiter' / 'SKILL.md'}")
    for line in done:
        print(line)
    print("Existing sessions pick up the hooks when they start or resume. Board: `arbiter open`.")


def uninstall(args):
    done = []
    if WINDOWS:
        if removed := remove_task():
            done.append(removed)
    elif (HOME / ".config/systemd/user" / UNIT).exists():
        systemctl("disable", "--now", UNIT)
        (HOME / ".config/systemd/user" / UNIT).unlink()
        systemctl("daemon-reload")
        done.append(f"stopped and removed the {UNIT} user service")
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
    for path in (claude / "settings.json", Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "hooks.json"):
        if set_hooks(path, None) is not None:
            done.append(f"removed Arbiter hooks from {path}")
    for root in skill_roots():
        if (root / "arbiter" / "SKILL.md").exists():
            shutil.rmtree(root / "arbiter")
            done.append(f"removed {root / 'arbiter'}")
    link = HOME / ".local/bin/arbiter"
    if not WINDOWS and link.is_symlink() and link.resolve() == entry():
        link.unlink()
        done.append(f"removed {link}")
    for line in done or ["nothing to remove"]:
        print(line)


def skill_roots():
    return (Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "skills",
            Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "skills")
