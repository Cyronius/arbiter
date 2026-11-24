"""Start a process group only after its identity has been committed by the scheduler."""
import os
import subprocess
import sys
from pathlib import Path


def tracked_popen(argv, acknowledge, **options):
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), str(read_fd), *argv],
                                pass_fds=(read_fd,), start_new_session=True, **options)
        try:
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


if __name__ == "__main__":
    fd = int(sys.argv[1])
    ack = os.read(fd, 1)
    os.close(fd)
    if ack != b"1":
        sys.exit(125)
    try:
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    except OSError as exc:
        # The exception may contain private arguments or paths.
        print(f"arbiter: command could not start ({exc.strerror})", file=sys.stderr)
        sys.exit(127 if isinstance(exc, FileNotFoundError) else 126)
