"""Extended window styles the island needs and Qt will not set for us.

Three problems, three fixes:

``WS_EX_NOACTIVATE``
    Without it, clicking the island takes focus away from whatever the user
    was typing in. An overlay that steals the caret is worse than no overlay,
    and this is the single most common defect in desktop widget projects.

``WS_EX_TOOLWINDOW``
    Keeps the island out of Alt-Tab and off the taskbar. ``Qt.Tool`` mostly
    covers this, but setting the style directly is reliable across the
    window being shown and hidden repeatedly.

Topmost re-assertion
    Windows quietly demotes topmost windows: a UAC prompt, a full-screen
    app exiting, or another always-on-top window can leave the island
    buried. Re-asserting on a cheap schedule is the accepted remedy.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

_user32 = ctypes.WinDLL("user32", use_last_error=True)

try:
    _get_long = _user32.GetWindowLongPtrW
    _set_long = _user32.SetWindowLongPtrW
except AttributeError:  # 32-bit Python
    _get_long = _user32.GetWindowLongW
    _set_long = _user32.SetWindowLongW

_get_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_long.restype = ctypes.c_ssize_t
_set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_long.restype = ctypes.c_ssize_t

_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL


def apply_overlay_styles(hwnd: int) -> bool:
    """Mark ``hwnd`` as a non-activating tool window. Idempotent."""
    if not hwnd:
        return False
    current = _get_long(hwnd, GWL_EXSTYLE)
    wanted = current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if wanted == current:
        return True
    return _set_long(hwnd, GWL_EXSTYLE, wanted) != 0 or True


def set_click_through(hwnd: int, enabled: bool) -> bool:
    """Let mouse input fall through the island to the window beneath.

    Enabled while the island is showing purely passive content, so the pill
    never eats a click aimed at a browser tab behind it. Disabled as soon as
    there is something to interact with.
    """
    if not hwnd:
        return False
    current = _get_long(hwnd, GWL_EXSTYLE)
    if enabled:
        wanted = current | WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        wanted = current & ~WS_EX_TRANSPARENT
    if wanted == current:
        return True
    _set_long(hwnd, GWL_EXSTYLE, wanted)
    return True


def reassert_topmost(hwnd: int) -> bool:
    """Push ``hwnd`` back to the top of the z-order without activating it."""
    if not hwnd:
        return False
    return bool(
        _user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )
    )
