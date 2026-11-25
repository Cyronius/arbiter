"""Read-only host probes for processes, CPU, memory and disk."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

AGENT_NAMES = ("claude", "codex")
WINDOWS = os.name == "nt"
if WINDOWS:
    from . import win32


def boot():
    if WINDOWS:
        return win32.boot()
    return (Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            time.time() - time.clock_gettime(time.CLOCK_BOOTTIME))


def proc_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name (index 0 is the state letter)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError, ValueError):
        return None


def start_time(pid: int) -> int | None:
    """A native creation timestamp that identifies one process when paired with its PID."""
    if WINDOWS:
        return win32.start_time(pid)
    fields = proc_stat(pid)
    try:
        return int(fields[19]) if fields else None
    except (IndexError, ValueError):
        return None


def alive(pid: int, started: int | None = None) -> bool:
    """True if `pid` runs, is not a zombie, and (when given) is the same process that started at `started`."""
    if WINDOWS:
        return win32.alive(pid, started)
    fields = proc_stat(pid)
    if not fields or fields[0] in ("Z", "X"):
        return False
    return started is None or (len(fields) > 19 and int(fields[19]) == started)


def processes():
    """Yield (pid, fields) for every process visible in /proc."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = proc_stat(int(entry.name))
            if fields:
                yield int(entry.name), fields


def group_alive(pgid: int, started: int | None = None) -> bool:
    """Ignore zombies; inspect the entire process group, not just its leader."""
    if WINDOWS:
        return win32.group_alive(pgid, started)
    if started is not None and (current := start_time(pgid)) is not None and current != started:
        return False
    for _, fields in processes():
        try:
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                return True
        except (IndexError, ValueError):
            pass
    return False


def comm(pid: int) -> str:
    if WINDOWS:
        return next((name for p, _, name in win32.processes() if p == pid), "")
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def parent(pid: int) -> int:
    fields = proc_stat(pid)
    try:
        return int(fields[1]) if fields else 0
    except (IndexError, ValueError):
        return 0


def agent_pid(env=None, start: int | None = None) -> int | None:
    """Find the Claude Code or Codex process this process runs under."""
    env = os.environ if env is None else env
    hinted = env.get("CLAUDE_PID")
    if hinted and hinted.isdigit() and alive(int(hinted)):
        return int(hinted)
    pid = start or os.getppid()
    table = {p: (name, ppid) for p, ppid, name in win32.processes()} if WINDOWS else None
    for _ in range(64):
        if pid <= 1:
            return None
        if table is None:
            name, ppid = comm(pid), parent(pid)
        else:
            name, ppid = table.get(pid, ("", 0))
            name = name.lower().removesuffix(".exe")
        if any(name == n or name.startswith(n + "-") for n in AGENT_NAMES):
            return pid if alive(pid) else None
        # Windows can reuse the PID of a parent that has already exited.
        if WINDOWS and ppid:
            child_start, parent_start = start_time(pid), start_time(ppid)
            if child_start is not None and parent_start is not None and parent_start > child_start:
                return None
        pid = ppid
    return None


def memory() -> dict:
    if WINDOWS:
        return win32.memory()
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return {"total_mb": int(info["MemTotal"].split()[0]) // 1024,
                "available_mb": int(info["MemAvailable"].split()[0]) // 1024,
                "load": list(os.getloadavg())}
    except (OSError, KeyError, ValueError):
        return {}


def gpu_pressure(drm=Path("/sys/class/drm")) -> list[str]:
    """Sample supported DRM driver counters without waking a suspended GPU."""
    reasons = []
    for card in drm.glob("card[0-9]*"):
        if not card.name[4:].isdigit():
            continue
        device = card / "device"
        try:
            if (device / "power/runtime_status").read_text().strip() == "suspended":
                continue
        except OSError:
            pass
        try:
            busy = int((device / "gpu_busy_percent").read_text())
            if busy > 90:
                reasons.append(f"{card.name} GPU {busy}% busy")
        except (OSError, ValueError):
            pass
        try:
            total = int((device / "mem_info_vram_total").read_text())
            used = int((device / "mem_info_vram_used").read_text())
            if total > 0 and used > .9 * total:
                reasons.append(f"{card.name} has under 10% free VRAM")
        except (OSError, ValueError):
            pass
    return reasons


def cpu_times() -> tuple[int, int] | None:
    """Cumulative busy and total CPU time in the host's native units."""
    if WINDOWS:
        return win32.cpu_times()
    try:
        values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values) - idle, sum(values)


class CpuMeter:
    """Whole-machine CPU busy percentage between successive calls."""

    def __init__(self):
        self.last = cpu_times()

    def sample(self) -> float | None:
        now = cpu_times()
        last, self.last = self.last, now
        if not now or not last or now[1] <= last[1]:
            return None
        return 100.0 * (now[0] - last[0]) / (now[1] - last[1])


def existing(path: str) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def disk(path: str) -> dict:
    """Filesystem of `path` (or its nearest existing parent): device ID and free GB."""
    p = existing(path)
    if WINDOWS:
        p = p.resolve()
        return {"device": win32.disk_device(p), "free_gb": shutil.disk_usage(p).free / 1e9}
    st = os.statvfs(p)
    return {"device": os.stat(p).st_dev, "free_gb": st.f_bavail * st.f_frsize / 1e9}


def size_gb(path: str, limit: int = 200000) -> float:
    """Apparent size of a file or tree, stopping after `limit` entries so huge trees stay cheap."""
    p = Path(path)
    try:
        if not p.exists():
            return 0.0
        if p.is_file():
            return p.stat().st_size / 1e9
    except OSError:
        return 0.0
    total, seen = 0, 0
    for root, dirs, files in os.walk(p):
        for f in files:
            seen += 1
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        if seen > limit:
            break
    return total / 1e9


class Probe:
    """What the scheduler may ask about the host. Tests substitute a fake."""
    alive = staticmethod(alive)
    start_time = staticmethod(start_time)
    group_alive = staticmethod(group_alive)
    disk = staticmethod(disk)
    size_gb = staticmethod(size_gb)


def oom_killed(pid: int, since: float) -> bool:
    """Require an OOM victim record for this PID and invocation in the current boot."""
    if WINDOWS:
        return False
    import subprocess
    try:
        result = subprocess.run(["journalctl", "--kernel", "--boot=0", "--quiet", "--no-pager",
                                 f"--since=@{since:.6f}", "--lines=1", "--output=cat",
                                 f"--grep=(Out of memory|Memory cgroup out of memory): Killed process {pid} "],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
        return result.returncode == 0 and f": Killed process {pid} ".encode() in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def process_cpu(excluded_pid, started):
    """Yield PID, creation time and consumed CPU seconds outside the measured workload."""
    if WINDOWS:
        yield from win32.process_cpu(excluded_pid, started)
        return
    tick = os.sysconf("SC_CLK_TCK")
    for pid, f in processes():
        try:
            if pid != os.getpid() and int(f[3]) != excluded_pid:
                yield pid, int(f[19]), (int(f[11]) + int(f[12])) / tick
        except (IndexError, ValueError):
            pass
