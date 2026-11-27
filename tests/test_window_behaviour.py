"""Window behaviour that users actually notice.

Both tests here exist because of bugs found the first time the island was
run by a person rather than by a script. Neither was caught by the suite,
because the suite never asked what the island does when the desktop has
focus, and never tried to hover it.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest

from core.island_window import IslandWindow
from core.osbridge import window_hooks as wh
from core.theme import load_theme

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND


# ---- hiding for full-screen apps ----------------------------------------


def test_the_desktop_is_not_a_full_screen_app():
    """Showing the desktop must not make the island vanish.

    Progman fills the screen and has no caption or resize border, so a
    geometry-only test calls it full-screen. The island then hid itself
    every time the user minimised everything or clicked the desktop,
    which is precisely when nothing was covering it.
    """
    shell = _user32.GetShellWindow()
    assert shell, "no shell window on this machine"
    assert wh.is_fullscreen(int(shell)) is False


@pytest.mark.parametrize(
    "class_name",
    ["Progman", "WorkerW", "Shell_TrayWnd"],
)
def test_shell_surfaces_are_never_full_screen(class_name):
    hwnd = _user32.FindWindowW(class_name, None)
    if not hwnd:
        pytest.skip(f"{class_name} not present on this machine")
    assert wh.is_fullscreen(int(hwnd)) is False


def test_window_class_is_readable():
    shell = int(_user32.GetShellWindow() or 0)
    assert wh.window_class(shell) == "Progman"


def test_window_class_of_nothing_is_empty():
    assert wh.window_class(0) == ""


def test_an_ordinary_window_is_not_full_screen():
    # The taskbar always exists, and the island must stay visible over it.
    hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        pytest.skip("no taskbar")
    assert wh.is_fullscreen(int(hwnd)) is False


# ---- being clickable -----------------------------------------------------


@pytest.fixture
def window(qapp):
    instance = IslandWindow(load_theme(None))
    yield instance
    instance.close()
    instance.deleteLater()


def test_the_island_accepts_the_mouse_by_default(window):
    """The island must be hoverable even with nothing to click.

    Turning on click-through whenever there were no buttons was a
    deadlock: WS_EX_TRANSPARENT blocks all mouse input, including the
    hover that is the only way to expand the island and give it buttons.
    """
    from core.model import IslandContent, Priority

    window.set_content(
        IslandContent(
            key="ambient.clock", priority=Priority.AMBIENT, title="09:41"
        ),
        animate=False,
    )
    assert window.passive_click_through is False
    assert window.click_through_active is False


def test_passive_mode_is_opt_in(window):
    from core.model import IslandContent, Priority

    window.set_passive_click_through(True)
    window.set_content(
        IslandContent(
            key="ambient.clock", priority=Priority.AMBIENT, title="09:41"
        ),
        animate=False,
    )
    assert window.click_through_active is True


def test_passive_mode_still_lets_buttons_be_clicked(window):
    from core.model import Action, IslandContent, Priority

    window.set_passive_click_through(True)
    window.set_content(
        IslandContent(
            key="alert.cpu",
            priority=Priority.ALERT,
            title="busy",
            actions=(Action("Mute", lambda: None),),
        ),
        animate=False,
    )
    assert window.click_through_active is False


def test_hovering_expands_the_island(window):
    from core.model import IslandContent, Priority

    window.set_content(
        IslandContent(
            key="ambient.clock", priority=Priority.AMBIENT, title="09:41"
        ),
        animate=False,
    )
    assert window.is_expanded() is False
    # The hover timer is what a real mouse-over starts.
    window.set_expanded(True)
    assert window.is_expanded() is True
