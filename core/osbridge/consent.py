r"""Detect which applications are using the microphone or camera right now.

Windows records every capability access under::

    HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager
         \ConsentStore\{microphone,webcam}

Each app gets a subkey holding ``LastUsedTimeStart`` and ``LastUsedTimeStop``
as FILETIMEs. While an app holds the device, ``Start`` is set and ``Stop`` is
zero. That is exactly the signal behind the privacy indicator Windows shows
in the system tray, and reading it gives us three things no process-name
allowlist can:

* **Meeting detection that is not a list of vendors.** Teams, Zoom, Discord,
  Slack huddles, a Google Meet tab in any browser, and whatever ships next
  year all light up the same key. The island switches to meeting mode
  because the microphone is live, which is the thing that actually matters.
* **Camera arbitration for the privacy guard.** Before the guard opens the
  webcam it checks whether another app already owns it, so the feature can
  never fight the user's video call for the device.
* **An honest privacy story.** We read a usage ledger the OS already keeps.
  We never need to enumerate windows or inspect audio content.

Updates are event-driven through ``RegNotifyChangeKeyValue``, with a polling
fallback if that call is unavailable.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import winreg
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

CONSENT_ROOT = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore"
)

MICROPHONE = "microphone"
WEBCAM = "webcam"
CAPABILITIES = (MICROPHONE, WEBCAM)

# Non-packaged (classic desktop) apps are keyed by their full path with the
# path separator replaced by "#", e.g. C:#Program Files#Zoom#bin#Zoom.exe
NONPACKAGED = "NonPackaged"

REG_NOTIFY_CHANGE_NAME = 0x00000001
REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
REG_NOTIFY_THREAD_AGNOSTIC = 0x10000000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_advapi32.RegNotifyChangeKeyValue.argtypes = [
    wintypes.HKEY,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.HANDLE,
    wintypes.BOOL,
]
_advapi32.RegNotifyChangeKeyValue.restype = wintypes.LONG
_kernel32.CreateEventW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD


@dataclass(frozen=True)
class CapabilityUser:
    """One app currently holding a capability."""

    capability: str
    # Raw registry subkey name, kept so the UI can show something exact.
    raw_key: str
    # Best-effort friendly name: executable name for desktop apps, package
    # family name for Store apps.
    name: str
    packaged: bool

    @property
    def is_browser(self) -> bool:
        return self.name.lower() in {
            "chrome.exe",
            "msedge.exe",
            "brave.exe",
            "firefox.exe",
            "opera.exe",
            "vivaldi.exe",
            "arc.exe",
        }


def _friendly(raw_key: str, packaged: bool) -> str:
    if packaged:
        # "Microsoft.Teams_8wekyb3d8bbwe" -> "Microsoft.Teams"
        return raw_key.split("_", 1)[0]
    # "C:#Program Files#Zoom#bin#Zoom.exe" -> "Zoom.exe"
    return raw_key.replace("#", os.sep).rsplit(os.sep, 1)[-1]


def _read_in_use(key: winreg.HKEYType, raw_key: str, capability: str,
                 packaged: bool) -> CapabilityUser | None:
    """Return a user when the subkey says the device is held right now."""
    try:
        with winreg.OpenKey(key, raw_key) as sub:
            try:
                start, _ = winreg.QueryValueEx(sub, "LastUsedTimeStart")
            except FileNotFoundError:
                return None
            try:
                stop, _ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
            except FileNotFoundError:
                stop = 0
    except OSError:
        return None
    # Held when a start time exists and no stop time has been written yet.
    if int(start or 0) > 0 and int(stop or 0) == 0:
        return CapabilityUser(
            capability=capability,
            raw_key=raw_key,
            name=_friendly(raw_key, packaged),
            packaged=packaged,
        )
    return None


def _subkeys(key: winreg.HKEYType) -> list[str]:
    out: list[str] = []
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            return out
        i += 1


def capability_users(capability: str) -> list[CapabilityUser]:
    """Apps currently using ``capability``. Empty on any error.

    Both HKCU and HKLM are consulted: services and apps running outside the
    interactive user's hive record under the machine root.
    """
    users: list[CapabilityUser] = []
    seen: set[str] = set()
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        path = CONSENT_ROOT + "\\" + capability
        try:
            with winreg.OpenKey(root, path) as cap:
                for raw in _subkeys(cap):
                    if raw == NONPACKAGED:
                        try:
                            with winreg.OpenKey(cap, NONPACKAGED) as np:
                                for raw2 in _subkeys(np):
                                    found = _read_in_use(
                                        np, raw2, capability, packaged=False
                                    )
                                    if found and found.raw_key not in seen:
                                        seen.add(found.raw_key)
                                        users.append(found)
                        except OSError:
                            continue
                    else:
                        found = _read_in_use(cap, raw, capability, packaged=True)
                        if found and found.raw_key not in seen:
                            seen.add(found.raw_key)
                            users.append(found)
        except OSError:
            continue
    return users


def snapshot() -> dict[str, list[CapabilityUser]]:
    return {cap: capability_users(cap) for cap in CAPABILITIES}


class _ConsentThread(QThread):
    """Waits on registry change notifications for both capability keys."""

    updated = Signal(dict)

    def __init__(self, poll_fallback_ms: int = 2000) -> None:
        super().__init__()
        self._stop_event = _kernel32.CreateEventW(None, True, False, None)
        self._poll_fallback_ms = poll_fallback_ms
        self._last: dict[str, tuple[str, ...]] = {}

    def request_stop(self) -> None:
        if self._stop_event:
            _kernel32.SetEvent(self._stop_event)

    def _emit_if_changed(self) -> None:
        snap = snapshot()
        fingerprint = {
            cap: tuple(sorted(u.raw_key for u in users))
            for cap, users in snap.items()
        }
        if fingerprint != self._last:
            self._last = fingerprint
            self.updated.emit(snap)

    def run(self) -> None:
        opened: list[winreg.HKEYType] = []
        events: list[int] = []
        try:
            for cap in CAPABILITIES:
                try:
                    key = winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        CONSENT_ROOT + "\\" + cap,
                        0,
                        winreg.KEY_NOTIFY | winreg.KEY_READ,
                    )
                except OSError:
                    continue
                evt = _kernel32.CreateEventW(None, True, False, None)
                if not evt:
                    key.Close()
                    continue
                opened.append(key)
                events.append(evt)

            self._emit_if_changed()

            if not events:
                # No notification possible: degrade to polling so the
                # feature still works rather than silently going dead.
                while True:
                    handles = (wintypes.HANDLE * 1)(self._stop_event)
                    rc = _kernel32.WaitForMultipleObjects(
                        1, handles, False, self._poll_fallback_ms
                    )
                    if rc == WAIT_OBJECT_0:
                        return
                    self._emit_if_changed()

            registered = [False] * len(events)
            while True:
                for i, (key, evt) in enumerate(zip(opened, events, strict=False)):
                    if registered[i]:
                        continue
                    rc = _advapi32.RegNotifyChangeKeyValue(
                        wintypes.HKEY(key.handle),
                        True,
                        REG_NOTIFY_CHANGE_NAME
                        | REG_NOTIFY_CHANGE_LAST_SET
                        | REG_NOTIFY_THREAD_AGNOSTIC,
                        wintypes.HANDLE(evt),
                        True,
                    )
                    registered[i] = rc == 0

                handles = (wintypes.HANDLE * (len(events) + 1))(
                    *events, self._stop_event
                )
                rc = _kernel32.WaitForMultipleObjects(
                    len(events) + 1, handles, False, self._poll_fallback_ms
                )
                if rc == WAIT_OBJECT_0 + len(events):
                    return
                if rc == WAIT_TIMEOUT:
                    # Safety sweep. Cheap, and covers a missed notification.
                    self._emit_if_changed()
                    continue
                idx = rc - WAIT_OBJECT_0
                if 0 <= idx < len(events):
                    _kernel32.ResetEvent(events[idx])
                    registered[idx] = False
                    self._emit_if_changed()
        finally:
            for evt in events:
                _kernel32.CloseHandle(wintypes.HANDLE(evt))
            for key in opened:
                with contextlib.suppress(OSError):
                    key.Close()


class ConsentWatcher(QObject):
    """Qt-friendly front end over :class:`_ConsentThread`."""

    # dict[str, list[CapabilityUser]]
    updated = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _ConsentThread | None = None
        self._latest: dict[str, list[CapabilityUser]] = {
            cap: [] for cap in CAPABILITIES
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = _ConsentThread()
        self._thread.updated.connect(self._on_update)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._thread.request_stop()
        self._thread.wait(2000)
        self._thread = None

    def _on_update(self, snap: dict) -> None:
        self._latest = snap
        self.updated.emit(snap)

    # ---- queries used by providers -----------------------------------

    def users(self, capability: str) -> list[CapabilityUser]:
        return list(self._latest.get(capability, ()))

    def microphone_live(self) -> bool:
        return bool(self._latest.get(MICROPHONE))

    def webcam_live(self) -> bool:
        return bool(self._latest.get(WEBCAM))

    def webcam_held_by_others(self, own_names: set[str]) -> bool:
        """True when some app other than us is using the camera.

        The privacy guard must back off in that case: grabbing the device
        mid-call would be worse than the problem the guard solves.
        """
        lowered = {n.lower() for n in own_names}
        return any(
            u.name.lower() not in lowered for u in self._latest.get(WEBCAM, ())
        )
