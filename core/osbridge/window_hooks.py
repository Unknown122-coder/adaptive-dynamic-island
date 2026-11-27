"""Event-driven foreground-window tracking.

The usual approach in Windows island clones is a 500 ms timer calling
``GetForegroundWindow``. That wakes the CPU 120 times a minute while the
user is reading a PDF, and still reacts up to half a second late.

Here the OS tells us instead. ``SetWinEventHook`` with
``WINEVENT_OUTOFCONTEXT`` posts events to the calling thread's message
queue, and Qt pumps the Win32 message queue on the GUI thread, so the
callback arrives inside the normal Qt event loop with no extra thread and
no polling. Idle cost is zero and latency is the OS's own.

Three hooks are installed:

``EVENT_SYSTEM_FOREGROUND``
    the active window changed.
``EVENT_SYSTEM_MINIMIZESTART`` / ``EVENT_SYSTEM_MINIMIZEEND``
    a window was minimised or restored, which changes what is in front
    without a foreground event always firing.
``EVENT_OBJECT_NAMECHANGE``
    the active window's *title* changed: a browser tab switch, a file
    opened in an editor. Events for any window other than the current
    foreground one are dropped in two comparisons.

A slow safety timer re-reads the foreground window every few seconds, so a
dropped hook (it happens, for example across a session switch) self-heals
instead of freezing the island's idea of context.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import psutil
from PySide6.QtCore import QObject, QTimer, Signal

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
MONITOR_DEFAULTTONEAREST = 0x00000002

_user32 = ctypes.WinDLL("user32", use_last_error=True)

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # hWinEventHook
    wintypes.DWORD,   # event
    wintypes.HWND,    # hwnd
    wintypes.LONG,    # idObject
    wintypes.LONG,    # idChild
    wintypes.DWORD,   # dwEventThread
    wintypes.DWORD,   # dwmsEventTime
)

_user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.UnhookWinEvent.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetShellWindow.restype = wintypes.HWND


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL

try:
    _get_window_long = _user32.GetWindowLongPtrW
except AttributeError:  # 32-bit Python has no ...PtrW variant
    _get_window_long = _user32.GetWindowLongW
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_ssize_t


def window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


# Shell surfaces that fill the screen and have no frame, so a naive
# geometry test calls them full-screen. The desktop is the important one:
# showing the desktop or minimising everything makes Progman the
# foreground window, and treating that as a full-screen app made the
# island vanish exactly when there was nothing covering it.
_SHELL_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "SysListView32",
        "#32769",
        "Windows.UI.Core.CoreWindow",
        "ForegroundStaging",
        "XamlExplorerHostIslandWindow",
    }
)


def is_fullscreen(hwnd: int) -> bool:
    """True when ``hwnd`` covers its whole monitor and has no window frame.

    Used to get the island out of the way of video and games. The style
    check matters: a maximised window also fills the monitor, but it keeps
    its caption and resize border, and hiding the island for a maximised
    editor would defeat the point of the app.
    """
    if not hwnd or not _user32.IsWindow(hwnd):
        return False
    # The desktop and the taskbar are not applications to hide behind.
    if window_class(hwnd) in _SHELL_CLASSES:
        return False
    shell = _user32.GetShellWindow()
    if shell and int(shell) == int(hwnd):
        return False
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    covers = (
        rect.left <= m.left
        and rect.top <= m.top
        and rect.right >= m.right
        and rect.bottom >= m.bottom
    )
    if not covers:
        return False
    style = _get_window_long(hwnd, GWL_STYLE)
    framed = bool(style & WS_CAPTION) and bool(style & WS_THICKFRAME)
    return not framed


@dataclass(frozen=True)
class ForegroundInfo:
    """A snapshot of what the user is looking at."""

    hwnd: int
    pid: int
    process: str          # lower-case executable name, e.g. "code.exe"
    exe_path: str | None
    title: str
    fullscreen: bool

    @property
    def stem(self) -> str:
        """Executable name without the extension, e.g. ``code``."""
        if self.process.endswith(".exe"):
            return self.process[:-4]
        return self.process


class ForegroundWatcher(QObject):
    """Emits :class:`ForegroundInfo` when the active window or title changes.

    Construct and :meth:`start` it on the GUI thread; it relies on Qt's
    Win32 message pump to deliver hook callbacks.
    """

    changed = Signal(object)

    def __init__(
        self,
        safety_interval_ms: int = 4000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._hooks: list[int] = []
        # The ctypes callback must outlive the hook or Windows will call
        # into freed memory. Holding a reference on the instance is the
        # whole trick, and forgetting it is the classic crash here.
        self._proc = WINEVENTPROC(self._on_event)
        self._last: ForegroundInfo | None = None
        self._pid_cache: dict[int, tuple[str, str | None]] = {}

        self._safety = QTimer(self)
        self._safety.setInterval(safety_interval_ms)
        self._safety.timeout.connect(self.poll)

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> bool:
        if self._hooks:
            return True
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        for first, last in (
            (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
            (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
            (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE),
        ):
            handle = _user32.SetWinEventHook(
                first, last, None, self._proc, 0, 0, flags
            )
            if handle:
                self._hooks.append(handle)
        self._safety.start()
        self.poll()
        return bool(self._hooks)

    def stop(self) -> None:
        self._safety.stop()
        for handle in self._hooks:
            _user32.UnhookWinEvent(handle)
        self._hooks.clear()

    @property
    def hooked(self) -> bool:
        return bool(self._hooks)

    def current(self) -> ForegroundInfo | None:
        return self._last

    # ---- internals ---------------------------------------------------

    def _on_event(self, _hook, event, hwnd, id_object, _id_child, _thread, _time):
        # Title-change events arrive for every window on the desktop. Drop
        # anything that is not the window the user is actually in, and drop
        # child-object noise (menu items, list rows) outright.
        if event == EVENT_OBJECT_NAMECHANGE:
            if id_object != OBJID_WINDOW:
                return
            if self._last is None or int(hwnd or 0) != self._last.hwnd:
                return
        self.poll()

    def _process_for(self, pid: int) -> tuple[str, str | None]:
        cached = self._pid_cache.get(pid)
        if cached is not None:
            return cached
        name: str = ""
        path: str | None = None
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or "").lower()
            try:
                path = proc.exe()
            except (psutil.AccessDenied, OSError):
                path = None
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            pass
        # Bound the cache: a long session churns through thousands of pids
        # and a leak here would show up directly in the idle memory budget.
        if len(self._pid_cache) > 256:
            self._pid_cache.clear()
        self._pid_cache[pid] = (name, path)
        return name, path

    def poll(self) -> None:
        """Read the foreground window now and emit if anything changed."""
        raw = _user32.GetForegroundWindow()
        hwnd = int(raw) if raw else 0
        if not hwnd:
            return
        pid = window_pid(hwnd)
        name, path = self._process_for(pid)
        info = ForegroundInfo(
            hwnd=hwnd,
            pid=pid,
            process=name,
            exe_path=path,
            title=window_title(hwnd),
            fullscreen=is_fullscreen(hwnd),
        )
        if info == self._last:
            return
        self._last = info
        self.changed.emit(info)
