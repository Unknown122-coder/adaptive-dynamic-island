"""Single-instance enforcement with a named Windows mutex.

A second copy of a top-most overlay is not a cosmetic problem: two islands
fight over the same strip of screen, both hold the same named pipe, and
both sample the webcam. A kernel mutex is the right tool because Windows
releases it even if the first process is killed, so a crash cannot leave a
stale lock file behind the way a file-based lock would.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class SingleInstance:
    """Holds the mutex for the lifetime of the process."""

    def __init__(self, name: str = "AdaptiveDynamicIsland") -> None:
        # "Local\\" scopes the mutex to the current session, so two users
        # on the same machine can each run their own island.
        self._name = f"Local\\{name}"
        self._handle: int | None = None
        self._already_running = False

    def acquire(self) -> bool:
        """True when this process owns the lock."""
        handle = _kernel32.CreateMutexW(None, False, self._name)
        last_error = ctypes.get_last_error()
        if not handle:
            # Without a handle we cannot tell; allow startup rather than
            # refusing to run at all.
            return True
        self._handle = handle
        self._already_running = last_error == ERROR_ALREADY_EXISTS
        return not self._already_running

    @property
    def already_running(self) -> bool:
        return self._already_running

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
