"""Start a process group only after its identity has been committed by the scheduler."""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGBREAK) if WINDOWS else (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def isolated_options(*, background=False):
    if WINDOWS:
        from .win32 import has_console
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if background or not has_console():
            flags |= subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def command_args(argv, cwd=None, env=None):
    """Resolve Windows executables and npm shims without passing job arguments through cmd.exe."""
    if not WINDOWS:
        return argv
    env = os.environ if env is None else env
    cwd = Path(cwd or os.getcwd())
    command = Path(argv[0])
    extensions = (".exe", ".com", ".cmd", ".bat", ".ps1")
    suffixes = ("",) if command.suffix.lower() in extensions else (".exe", ".com", ".cmd", ".bat")
    folders = [cwd] if command.drive or command.parent != Path(".") or argv[0].startswith(".") else [
        cwd, *(cwd / directory.strip('"') for directory in os.get_exec_path(env) if directory)
    ]
    executable = next((file for folder in folders for suffix in suffixes
                       if (file := folder / (str(command) + suffix)).is_file()), None)
    if executable is None:
        raise FileNotFoundError(2, "executable was not found")
    if executable.suffix.lower() not in (".cmd", ".bat"):
        if executable.suffix.lower() == ".ps1":
            raise OSError("Run PowerShell scripts through powershell.exe or pwsh.exe with -File")
        return [str(executable), *argv[1:]]
    shim = executable.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'"%_prog%"\s+"%dp0%\\([^"%\r\n]+\.[cm]?js)"\s+%\*', shim)
    npm_cli = re.search(r'SET "[A-Z_]+=%~dp0\\(node_modules\\npm\\bin\\(?:npm|npx)-cli\.js)"', shim)
    if executable.name.lower() in ("npm.cmd", "npx.cmd") and npm_cli:
        script = executable.parent / npm_cli[1]
    elif match:
        script = executable.parent / match[1]
    else:
        raise OSError("Run batch scripts through an explicit cmd.exe command")
    node = executable.with_name("node.exe")
    node = str(node) if node.is_file() else shutil.which("node.exe", path=env.get("PATH"))
    if not node or not script.is_file():
        raise FileNotFoundError(2, "Node.js or the npm shim target was not found")
    return [node, str(script), *argv[1:]]


def signal_group(proc, sig, *, force=False):
    if not WINDOWS:
        return os.killpg(proc.pid, signal.SIGKILL if force else sig)
    from .win32 import has_console, terminate_group
    if not force and has_console():
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            return
        except OSError:  # A background job may have no console.
            pass
    if getattr(proc, "arbiter_started", None) is None:
        return proc.kill()
    terminate_group(proc.pid, proc.arbiter_started, 128 + sig)


def work_alive(proc):
    from .system import group_alive
    return proc.poll() is None or group_alive(proc.pid, getattr(proc, "arbiter_started", None))


def exit_code(code):
    if WINDOWS:
        # CPython's Windows exit path takes a signed C long; preserve the full DWORD.
        return code if code < 2**31 else code - 2**32
    return code if code >= 0 else 128 - code


def wait_for_job(proc):
    from .win32 import group_alive, start_time
    proc.arbiter_started = start_time(proc.pid)
    deadline = time.monotonic() + 10
    while proc.arbiter_started is not None and proc.poll() is None and time.monotonic() < deadline:
        if group_alive(proc.pid, proc.arbiter_started):
            return
        time.sleep(.01)
    raise OSError("Cannot initialize the Windows Job Object")


def tracked_popen(argv, acknowledge, **options):
    read_fd, write_fd = os.pipe()
    try:
        gate = read_fd
        if WINDOWS:
            import msvcrt
            gate = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(gate, True)
            inheritance = {"startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": [gate]})}
        else:
            inheritance = {"pass_fds": (read_fd,)}
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), str(gate), *argv],
                                **inheritance, **isolated_options(), **options)
        try:
            if WINDOWS:
                wait_for_job(proc)
            acknowledge(proc.pid)
            os.write(write_fd, b"1")
        except BaseException:
            os.close(write_fd)
            write_fd = None
            proc.wait()
            raise
        return proc
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def acknowledged(fd):
    with os.fdopen(fd, "rb", buffering=0) as gate:
        return gate.read(1) == b"1"


def windows_launch(gate, argv):
    import msvcrt
    from arbiter.win32 import active_processes, has_console, kill_on_close, own_job

    fd = msvcrt.open_osfhandle(gate, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    started = False

    def on_break(*_):
        if not started:
            raise SystemExit(130)

    signal.signal(signal.SIGBREAK, on_break)
    with own_job() as job:
        try:
            if not acknowledged(fd):
                return 125
            flags = 0 if has_console() else subprocess.CREATE_NO_WINDOW
            with subprocess.Popen(command_args(argv), creationflags=flags) as child:
                started = True
                code = child.wait()
            # Keep the named object reachable after the command exits if a descendant remains.
            while active_processes(job) > 1:
                time.sleep(.1)
            return exit_code(code)
        finally:
            if active_processes(job) == 1:
                kill_on_close(job, False)


if __name__ == "__main__":
    gate = int(sys.argv[1])
    argv = sys.argv[2:]
    try:
        if WINDOWS:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            sys.exit(windows_launch(gate, argv))
        if not acknowledged(gate):
            sys.exit(125)
        os.execvpe(argv[0], argv, os.environ)
    except OSError as exc:
        # The exception may contain private arguments or paths.
        print(f"arbiter: command could not start ({exc.strerror or exc})", file=sys.stderr)
        sys.exit(127 if isinstance(exc, FileNotFoundError) else 126)
