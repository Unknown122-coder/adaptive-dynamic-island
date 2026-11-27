"""User settings: typed access, deep-merged defaults, atomic saves.

Settings are read with dotted paths (``settings.get("privacy.enabled")``)
so a provider can ask for exactly what it needs without importing a schema
object. Unknown keys in the user's file are preserved rather than dropped,
which matters when a user downgrades after trying a newer build.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from core import paths

DEFAULTS: dict[str, Any] = {
    "theme": "midnight_oled",
    "display": {
        # "internal" anchors to the built-in laptop panel, "primary" to the
        # Windows primary screen, or give an exact Qt screen name.
        "preference": "internal",
        # Nudge right (+) or left (-) for laptops with an off-centre camera.
        "horizontal_offset": 0,
    },
    "behaviour": {
        # Get out of the way of full-screen video and games.
        "hide_on_fullscreen": True,
        "start_with_windows": False,
        # Seconds of ambient content before the island collapses to its
        # smallest state. 0 keeps it always visible.
        "idle_collapse_seconds": 0,
    },
    "providers": {
        "clock": True,
        "context": True,
        "activities": True,
        "drop": True,
        "plugins": True,
        "clipboard": False,
        "sentinel": False,
        "privacy": False,
    },
    # Per-plugin settings live under plugins.<id>, for example
    # {"plugins": {"pomodoro": {"enabled": true, "focus_minutes": 50}}}.
    # A plugin reads its own block through ctx.setting().
    "plugins": {},
    "clipboard": {
        # How many recent items to keep. Memory only; never written to disk.
        "stack_size": 5,
        # Seconds before the clipboard is cleared automatically after a
        # credential is copied. 0 disables it and offers a manual wipe
        # instead, which is the safer default: silently clearing something
        # the user is about to paste would be its own failure.
        "auto_wipe_seconds": 0,
    },
    "drop": {
        # Copy extracted text to the clipboard automatically. Off would mean
        # the user has to click, which defeats the point of a drag.
        "copy_text_automatically": True,
        # Outputs are always written beside the original, never over it.
        "jpeg_quality": 82,
        "webp_quality": 85,
    },
    "context": {
        # Treat the microphone being live as "in a meeting", regardless of
        # which application holds it.
        "meeting_from_microphone": True,
        "show_git_branch": True,
        "developer_processes": [
            "code.exe",
            "devenv.exe",
            "pycharm64.exe",
            "idea64.exe",
            "windowsterminal.exe",
            "powershell.exe",
            "pwsh.exe",
            "cmd.exe",
            "wezterm-gui.exe",
            "alacritty.exe",
        ],
        "browser_processes": [
            "chrome.exe",
            "msedge.exe",
            "brave.exe",
            "firefox.exe",
            "opera.exe",
            "vivaldi.exe",
            "arc.exe",
        ],
    },
    "activities": {
        # Named pipe the Live Activity API listens on. Per-user by default.
        "pipe_name": "AdaptiveIsland",
        # An activity with no update for this long is assumed dead and
        # retracted, so a crashed producer cannot pin content on screen.
        "stale_seconds": 90,
        "completed_linger_seconds": 6,
    },
    "sentinel": {
        "sample_interval_seconds": 3,
        # Whole-machine percentages, matching the way Task Manager reports
        # them rather than the per-core figure psutil returns.
        "cpu_percent": 85,
        "cpu_sustain_seconds": 10,
        "memory_percent": 92,
        "memory_sustain_seconds": 15,
        # Long enough that a Wi-Fi roam between access points does not
        # announce an outage that lasted two seconds.
        "network_sustain_seconds": 6,
        # 0 disables the disk check.
        "disk_free_percent": 5,
        "mute_minutes": 10,
        # The End task button is confirm-on-second-click. Set false to
        # remove it entirely and keep the alert informational.
        "offer_end_task": True,
    },
    "privacy": {
        "enabled": False,
        # The camera is opened only while one of these is the foreground
        # window, or while the user has explicitly asked the guard to
        # watch. Password managers are the defensible default: the moment
        # a vault is open is exactly when a second pair of eyes matters.
        "sensitive_processes": [
            "1password.exe",
            "bitwarden.exe",
            "keepass.exe",
            "keepassxc.exe",
            "lastpass.exe",
            "dashlane.exe",
            "enpass.exe",
            "protonpass.exe",
        ],
        "frames_per_second": 1.0,
        # How many faces constitute "you are not alone".
        "faces_to_warn": 2,
        # Consecutive agreeing frames needed to raise and to clear a
        # warning. Hysteresis in both directions, so one bad frame can
        # neither interrupt the user nor silence a real warning.
        "confirm_frames": 3,
        "clear_frames": 2,
        # 0 disables walk-away detection entirely.
        "walk_away_seconds": 60,
        # How long a dismissed warning stays quiet. Without this, a
        # dismissed warning that is still true reappears at once, which
        # teaches people to turn the feature off.
        "dismiss_cooldown_seconds": 120,
    },
    "advanced": {
        "log_level": "INFO",
    },
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    """Live settings object. Mutations are explicit via :meth:`set`."""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self._path = path

    # ---- construction ------------------------------------------------

    @classmethod
    def load(cls) -> Settings:
        """Read user settings, layered over shipped defaults.

        Order, lowest first: built-in DEFAULTS, the shipped
        ``config/settings.json``, then the user's own file.
        """
        data = copy.deepcopy(DEFAULTS)
        for candidate in (paths.default_settings_file(), paths.settings_file()):
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = _deep_merge(data, raw)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                print(f"[config] ignoring {candidate.name}: {exc}")
        return cls(data, paths.settings_file())

    # ---- access ------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def toggle(self, dotted: str) -> bool:
        value = not bool(self.get(dotted, False))
        self.set(dotted, value)
        return value

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def number(self, dotted: str, default: float) -> float:
        """A numeric setting, falling back only when it is missing or junk.

        The obvious shorthand, ``settings.get(key, default) or default``,
        is wrong and silently so: zero is falsy, so a user who sets a
        threshold to 0 to switch something off gets the default back
        instead. Every "this setting does nothing" bug report starts here.
        """
        value = self.get(dotted, None)
        if value is None or isinstance(value, bool):
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def integer(self, dotted: str, default: int) -> int:
        return int(self.number(dotted, float(default)))

    def lowered_list(self, dotted: str) -> set[str]:
        """A settings list normalised for case-insensitive matching."""
        raw = self.get(dotted, []) or []
        return {str(item).lower() for item in raw}

    # ---- persistence -------------------------------------------------

    def save(self) -> bool:
        """Write settings atomically.

        A half-written settings file on a power cut would make the app fail
        to start, so the new content goes to a temporary file in the same
        directory and is then moved into place.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, indent=2, ensure_ascii=False)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".settings-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True
        except OSError as exc:
            print(f"[config] could not save settings: {exc}")
            return False

    @property
    def path(self) -> Path:
        return self._path
