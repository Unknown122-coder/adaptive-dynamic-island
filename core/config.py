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
        "clipboard": False,
        "sentinel": False,
        "privacy": False,
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
        "cpu_percent": 85,
        "cpu_sustain_seconds": 10,
        "sample_interval_seconds": 2,
    },
    "privacy": {
        "enabled": False,
        # Only sample the webcam while one of these is in front.
        "sensitive_processes": [],
        "frames_per_second": 1.0,
        "walk_away_seconds": 60,
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
