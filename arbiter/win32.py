"""Native Windows process accounting and filesystem primitives."""
from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import msvcrt
import os

kernel = c.WinDLL("kernel32", use_last_error=True)
advapi = c.WinDLL("advapi32", use_last_error=True)
ntdll = c.WinDLL("ntdll", use_last_error=True)
INVALID_HANDLE = w.HANDLE(-1).value


def api(library, name, result, *args):
    fn = getattr(library, name)
    fn.restype, fn.argtypes = result, args
    return fn


close = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
open_process = api(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
process_times = api(kernel, "GetProcessTimes", w.BOOL, w.HANDLE, *(c.POINTER(w.FILETIME),) * 4)
wait = api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
system_times = api(kernel, "GetSystemTimes", w.BOOL, *(c.POINTER(w.FILETIME),) * 3)
snapshot = api(kernel, "CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
has_console = api(kernel, "GetConsoleCP", w.UINT)
current_process = api(kernel, "GetCurrentProcess", w.HANDLE)
create_job = api(kernel, "CreateJobObjectW", w.HANDLE, c.c_void_p, w.LPCWSTR)
open_job = api(kernel, "OpenJobObjectW", w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
assign_job = api(kernel, "AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
query_job = api(kernel, "QueryInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p)
set_job = api(kernel, "SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
terminate_job = api(kernel, "TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
in_job = api(kernel, "IsProcessInJob", w.BOOL, w.HANDLE, w.HANDLE, c.POINTER(w.BOOL))
query_system = api(ntdll, "NtQuerySystemInformation", w.LONG, c.c_int, c.c_void_p, w.ULONG, c.c_void_p)
volume_path = api(kernel, "GetVolumePathNameW", w.BOOL, w.LPCWSTR, w.LPWSTR, w.DWORD)
volume_name = api(kernel, "GetVolumeNameForVolumeMountPointW", w.BOOL, w.LPCWSTR, w.LPWSTR, w.DWORD)


class ProcessEntry(c.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_size_t),
                ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", w.LONG),
                ("flags", w.DWORD), ("name", w.WCHAR * 260)]


first_process = api(kernel, "Process32FirstW", w.BOOL, w.HANDLE, c.POINTER(ProcessEntry))
next_process = api(kernel, "Process32NextW", w.BOOL, w.HANDLE, c.POINTER(ProcessEntry))


class MemoryStatus(c.Structure):
    _fields_ = [("size", w.DWORD), ("load", w.DWORD), *((name, c.c_ulonglong) for name in (
        "total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended"))]


memory_status = api(kernel, "GlobalMemoryStatusEx", w.BOOL, c.POINTER(MemoryStatus))


class JobAccounting(c.Structure):
    _fields_ = [("user_time", c.c_longlong), ("kernel_time", c.c_longlong), ("period_user", c.c_longlong),
                ("period_kernel", c.c_longlong), ("faults", w.DWORD), ("total", w.DWORD),
                ("active", w.DWORD), ("terminated", w.DWORD)]


class JobLimits(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", w.DWORD),
                ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t), ("active_limit", w.DWORD),
                ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class JobExtendedLimits(c.Structure):
    _fields_ = [("basic", JobLimits), ("io", c.c_ulonglong * 6), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


@contextmanager
def closing(handle):
    try:
        yield handle
    finally:
        if handle and handle != INVALID_HANDLE:
            close(handle)


def filetime(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


@lru_cache(maxsize=1)
def boot():
    # SystemTimeOfDayInformation begins with the kernel boot timestamp (Windows FILETIME).
    data = (c.c_longlong * 6)()
    if query_system(3, data, c.sizeof(data), None) != 0:
        raise OSError("Cannot read the Windows kernel boot time")
    return f"windows:{data[0]}", data[0] / 10_000_000 - 11_644_473_600


def times(handle):
    created, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
    if process_times(handle, c.byref(created), c.byref(exited), c.byref(kernel_time), c.byref(user_time)):
        return filetime(created), filetime(kernel_time) + filetime(user_time)
    return None


def start_time(pid):
    if not 0 < pid <= 0xffffffff:
        return None
    with closing(open_process(0x1000, False, pid)) as handle:  # PROCESS_QUERY_LIMITED_INFORMATION
        value = times(handle) if handle else None
        return value[0] if value else None


def alive(pid, started=None):
    if not 0 < pid <= 0xffffffff:
        return False
    with closing(open_process(0x101000, False, pid)) as handle:  # also SYNCHRONIZE
        if not handle or wait(handle, 0) != 258:  # WAIT_TIMEOUT
            return False
        if started is None:
            return True
        value = times(handle)
        return value is not None and value[0] == started


def processes():
    with closing(snapshot(2, 0)) as handle:  # TH32CS_SNAPPROCESS
        if handle == INVALID_HANDLE:
            return
        entry = ProcessEntry(size=c.sizeof(ProcessEntry))
        more = first_process(handle, c.byref(entry))
        while more:
            yield entry.pid, entry.parent, entry.name
            more = next_process(handle, c.byref(entry))


def job_name(pid, started):
    return f"Global\\Arbiter-{pid}-{started}"


def kill_on_close(job, enabled):
    limits = JobExtendedLimits()
    limits.basic.flags = 0x2000 if enabled else 0  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not set_job(job, 9, c.byref(limits), c.sizeof(limits)):
        raise c.WinError(c.get_last_error())


@contextmanager
def own_job():
    pid = os.getpid()
    c.set_last_error(0)
    with closing(create_job(None, job_name(pid, start_time(pid)))) as job:
        if not job or c.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            raise c.WinError(c.get_last_error())
        kill_on_close(job, True)
        if not assign_job(job, current_process()):
            raise c.WinError(c.get_last_error())
        yield job


def active_processes(job):
    info = JobAccounting()
    if not query_job(job, 1, c.byref(info), c.sizeof(info), None):
        raise c.WinError(c.get_last_error())
    return info.active


def group_alive(pid, started=None):
    started = started if started is not None else start_time(pid)
    if started is None:
        return False
    with closing(open_job(4, False, job_name(pid, started))) as job:  # JOB_OBJECT_QUERY
        if not job:
            error = c.get_last_error()
            if error == 2:  # ERROR_FILE_NOT_FOUND
                return False
            raise c.WinError(error)
        return active_processes(job) > 0


def terminate_group(pid, started, code):
    with closing(open_job(8, False, job_name(pid, started))) as job:  # JOB_OBJECT_TERMINATE
        if not job:
            if c.get_last_error() == 2:
                raise ProcessLookupError(pid)
            raise c.WinError(c.get_last_error())
        if not terminate_job(job, code):
            raise c.WinError(c.get_last_error())


def process_cpu(excluded_pid, started):
    name = job_name(excluded_pid, started)
    with closing(open_job(4, False, name)) as job:
        for pid, _, _ in processes():
            if pid in (0, os.getpid()):
                continue
            with closing(open_process(0x1000, False, pid)) as handle:
                if not handle:
                    continue
                member = w.BOOL()
                if job and in_job(handle, job, c.byref(member)) and member.value:
                    continue
                value = times(handle)
                if value:
                    yield pid, value[0], value[1] / 10_000_000


def memory():
    value = MemoryStatus(size=c.sizeof(MemoryStatus))
    if memory_status(c.byref(value)):
        return {"total_mb": value.total // 1048576, "available_mb": value.available // 1048576}
    return {}


def cpu_times():
    idle, system, user = (w.FILETIME() for _ in range(3))
    if system_times(c.byref(idle), c.byref(system), c.byref(user)):
        total = filetime(system) + filetime(user)
        return total - filetime(idle), total
    return None


def disk_device(path):
    root = c.create_unicode_buffer(32768)
    if not volume_path(os.path.abspath(path), root, len(root)):
        raise c.WinError(c.get_last_error())
    volume = c.create_unicode_buffer(64)
    return (volume.value if volume_name(root.value, volume, len(volume)) else root.value).casefold()


def lock_file(file):
    file.seek(0)
    try:
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.winerror == 33 or exc.errno == 13:
            raise BlockingIOError from exc
        raise


def protect(path, *, directory=False):
    """Restrict access to the owner and SYSTEM, with inheritance for a state directory."""
    convert = api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                  w.BOOL, w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p)
    set_security = api(advapi, "SetFileSecurityW", w.BOOL, w.LPCWSTR, w.DWORD, c.c_void_p)
    free = api(kernel, "LocalFree", c.c_void_p, c.c_void_p)
    descriptor = c.c_void_p()
    # Protected DACL, inherited by child folders and files. OW is the object owner's SID.
    inherit = "OICI" if directory else ""
    if not convert(f"D:P(A;{inherit};FA;;;OW)(A;{inherit};FA;;;SY)", 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        if not set_security(str(path), 0x80000004, descriptor):
            raise c.WinError(c.get_last_error())
    finally:
        free(descriptor)


def open_shared_file(path):
    """Allow atomic replacement while reading a regular file without following a leaf reparse point."""
    create = api(kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE)
    file_type = api(kernel, "GetFileType", w.DWORD, w.HANDLE)

    class AttributeTag(c.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

    file_info = api(kernel, "GetFileInformationByHandleEx", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    try:
        info = AttributeTag()
        if (file_type(handle) != 1 or not file_info(handle, 9, c.byref(info), c.sizeof(info))
                or info.attributes & 0x410):  # FILE_ATTRIBUTE_REPARSE_POINT or DIRECTORY
            raise OSError("Expected a regular file")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise
    return os.fdopen(fd, "rb")
