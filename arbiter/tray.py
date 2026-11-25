"""Arbiter's Windows notification-area icon."""
import ctypes as c
from ctypes import wintypes as w
from hashlib import sha256
import os
import struct
import threading
import traceback
import webbrowser

from .client import Client, state_dir
from .win32 import api, closing, kernel

user = c.WinDLL("user32", use_last_error=True)
shell = c.WinDLL("shell32", use_last_error=True)
WindowProc = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

WM_CLOSE, WM_DESTROY, WM_TIMER = 0x0010, 0x0002, 0x0113
WM_CONTEXTMENU, WM_LBUTTONUP, WM_RBUTTONUP = 0x007b, 0x0202, 0x0205
TRAY_EVENT, NIN_SELECT, NIN_KEYSELECT = 0x8001, 0x0400, 0x0401
NIM_ADD, NIM_DELETE, NIM_SETFOCUS, NIM_SETVERSION = 0, 2, 3, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 1, 2, 4, 0x80
OPEN, SETTINGS, EXIT = 1, 2, 3
CLASS_NAME = "ArbiterTray"


class WindowClass(c.Structure):
    _fields_ = [("style", w.UINT), ("proc", WindowProc), ("class_extra", c.c_int), ("window_extra", c.c_int),
                ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
                ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]


class NotifyIconData(c.Structure):
    _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT), ("flags", w.UINT),
                ("message", w.UINT), ("icon", w.HICON), ("tip", w.WCHAR * 128),
                ("state", w.DWORD), ("state_mask", w.DWORD), ("info", w.WCHAR * 256),
                ("version", w.UINT), ("title", w.WCHAR * 64), ("info_flags", w.DWORD),
                ("guid", w.BYTE * 16), ("balloon_icon", w.HICON)]


module_handle = api(kernel, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
create_mutex = api(kernel, "CreateMutexW", w.HANDLE, c.c_void_p, w.BOOL, w.LPCWSTR)
register_class = api(user, "RegisterClassW", w.WORD, c.POINTER(WindowClass))
unregister_class = api(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
create_window = api(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                    c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p)
default_proc = api(user, "DefWindowProcW", c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
destroy_window = api(user, "DestroyWindow", w.BOOL, w.HWND)
register_message = api(user, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
post_message = api(user, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
post_quit = api(user, "PostQuitMessage", None, c.c_int)
set_timer = api(user, "SetTimer", c.c_size_t, w.HWND, c.c_size_t, w.UINT, c.c_void_p)
get_message = api(user, "GetMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
dispatch_message = api(user, "DispatchMessageW", c.c_ssize_t, c.POINTER(w.MSG))
make_icon = api(user, "CreateIconFromResourceEx", w.HICON, c.c_void_p, w.DWORD, w.BOOL,
                w.DWORD, c.c_int, c.c_int, w.UINT)
destroy_icon = api(user, "DestroyIcon", w.BOOL, w.HICON)
notify_icon = api(shell, "Shell_NotifyIconW", w.BOOL, w.DWORD, c.POINTER(NotifyIconData))
create_menu = api(user, "CreatePopupMenu", w.HMENU)
append_menu = api(user, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
track_menu = api(user, "TrackPopupMenu", w.BOOL, w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.c_void_p)
destroy_menu = api(user, "DestroyMenu", w.BOOL, w.HMENU)
foreground_window = api(user, "SetForegroundWindow", w.BOOL, w.HWND)
cursor_pos = api(user, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
message_box = api(user, "MessageBoxW", c.c_int, w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)


def arbiter_icon():
    """Rasterize the board's three-bar mark as a small alpha-channel Windows icon."""
    size = 32
    pixels, mask = bytearray(), bytearray()
    for y in reversed(range(size)):
        mask_row = 0
        inset = max(0, 4 - min(y, size - 1 - y))
        slant = (26 - y) // 4
        for x in range(size):
            if x < inset or x >= size - inset:
                pixels.extend((0, 0, 0, 0))
                mask_row |= 1 << (size - 1 - x)
                continue
            bar = any(left + slant <= x < left + slant + 4 and top <= y < 26
                      for left, top in ((5, 17), (12, 11), (19, 5)))
            pixels.extend((236, 213, 139, 255) if bar else (57, 40, 19, 255))
        mask.extend(mask_row.to_bytes(4, "big"))
    bitmap_header = struct.pack("<IiiHH6I", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    bitmap = bitmap_header + pixels + mask
    return make_icon(bitmap, len(bitmap), True, 0x00030000, size, size, 0)


class Tray:
    def __init__(self, client=None):
        self.client = client
        self.stopping = threading.Event()
        self.hwnd = None
        self.icon = None
        self.registered = self.modern = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run_background, name="arbiter-tray", daemon=True)
        self.thread.start()

    def run_background(self):
        try:
            self.run()
        except (OSError, RuntimeError):
            traceback.print_exc()

    def stop(self):
        self.stopping.set()
        if hwnd := self.hwnd:
            post_message(hwnd, WM_CLOSE, 0, 0)
        if self.thread:
            self.thread.join(timeout=3)

    def run(self):
        state = os.path.normcase(str(state_dir().resolve())).encode("utf-8")
        name = "Local\\ArbiterTray-" + sha256(state).hexdigest()
        c.set_last_error(0)
        with closing(create_mutex(None, False, name)) as mutex:
            if not mutex:
                raise c.WinError(c.get_last_error())
            if c.get_last_error() == 183:
                return
            self.run_window()

    def run_window(self):
        instance = module_handle(None)
        self.taskbar_created = register_message("TaskbarCreated")
        window_class = WindowClass(proc=WindowProc(self.window_proc), instance=instance, name=CLASS_NAME)
        if not register_class(c.byref(window_class)):
            raise c.WinError(c.get_last_error())
        try:
            self.icon = arbiter_icon()
            if not self.icon:
                raise c.WinError(c.get_last_error())
            # A hidden top-level window receives TaskbarCreated when Explorer restarts.
            hwnd = create_window(0, CLASS_NAME, "Arbiter tray", 0, 0, 0, 0, 0, None, None, instance, None)
            if not hwnd:
                raise c.WinError(c.get_last_error())
            self.hwnd = hwnd
            self.data = NotifyIconData(size=c.sizeof(NotifyIconData), hwnd=hwnd, id=1,
                                       flags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP,
                                       message=TRAY_EVENT, icon=self.icon, tip="Arbiter")
            if self.stopping.is_set():
                return
            self.add_icon()
            if not set_timer(hwnd, 1, 2000, None):
                raise c.WinError(c.get_last_error())
            message = w.MSG()
            while result := get_message(c.byref(message), None, 0, 0):
                if result == -1:
                    raise c.WinError(c.get_last_error())
                dispatch_message(c.byref(message))
        finally:
            if self.hwnd:
                self.remove_icon()
                destroy_window(self.hwnd)
            if self.icon:
                destroy_icon(self.icon)
            unregister_class(CLASS_NAME, instance)

    def add_icon(self):
        self.registered = bool(notify_icon(NIM_ADD, c.byref(self.data)))
        if self.registered:
            self.data.version = 4
            self.modern = bool(notify_icon(NIM_SETVERSION, c.byref(self.data)))

    def remove_icon(self):
        if self.registered:
            notify_icon(NIM_DELETE, c.byref(self.data))
            self.registered = False

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == TRAY_EVENT:
            event = lparam & 0xffff
            if event in (NIN_SELECT, NIN_KEYSELECT) or (not self.modern and event == WM_LBUTTONUP):
                self.open_board(hwnd)
            elif event == WM_CONTEXTMENU or (not self.modern and event == WM_RBUTTONUP):
                self.show_menu(hwnd, wparam)
            return 0
        if self.hwnd and message == self.taskbar_created:
            self.remove_icon()
            self.add_icon()
            return 0
        if self.hwnd and message == WM_TIMER and not self.registered:
            self.add_icon()
            return 0
        if message == WM_CLOSE:
            self.remove_icon()
            destroy_window(hwnd)
            return 0
        if message == WM_DESTROY:
            self.hwnd = None
            post_quit(0)
            return 0
        return default_proc(hwnd, message, wparam, lparam)

    def show_menu(self, hwnd, anchor):
        menu = create_menu()
        if not menu:
            return
        try:
            append_menu(menu, 0, OPEN, "Open Arbiter")
            append_menu(menu, 0, SETTINGS, "Settings...")
            append_menu(menu, 0x800, 0, None)  # MF_SEPARATOR
            append_menu(menu, 0, EXIT, "Exit tray")
            point = w.POINT(c.c_short(anchor & 0xffff).value, c.c_short((anchor >> 16) & 0xffff).value)
            if not self.modern or (point.x, point.y) in ((0, 0), (-1, -1)):
                cursor_pos(c.byref(point))
            foreground_window(hwnd)
            action = track_menu(menu, 0x0102, point.x, point.y, 0, hwnd, None)  # TPM_RETURNCMD | TPM_RIGHTBUTTON
            post_message(hwnd, 0, 0, 0)  # Let a dismissed tray menu fully release its foreground owner.
            notify_icon(NIM_SETFOCUS, c.byref(self.data))
            if action == EXIT:
                post_message(hwnd, WM_CLOSE, 0, 0)
            elif action in (OPEN, SETTINGS):
                self.open_board(hwnd, settings=action == SETTINGS)
        finally:
            destroy_menu(menu)

    def open_board(self, hwnd, *, settings=False):
        try:
            client = self.client or Client(timeout=2)
            url = client.board_link(tab="settings" if settings else None)
            if not webbrowser.open(url):
                raise OSError("Windows could not open the default browser.")
        except (RuntimeError, OSError, ValueError) as exc:
            message_box(hwnd, f"Could not open the Arbiter board.\n\n{exc}", "Arbiter", 0x30)
