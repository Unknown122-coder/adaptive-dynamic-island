"""Where the app keeps its files.

Separating *shipped defaults* from *user state* is what makes the installer
behave. Defaults live next to the code and are replaced wholesale on every
upgrade; user settings, custom themes and third-party plugins live under
``%APPDATA%`` and must survive an upgrade untouched.

It also means the installed copy under ``Program Files`` never needs write
access, so the app runs without elevation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AdaptiveIsland"


def is_frozen() -> bool:
    """True when running from a Nuitka or PyInstaller build."""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def app_root() -> Path:
    """Directory holding the shipped code and default assets."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_dir() -> Path:
    """Per-user writable directory, created on demand."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return user_dir() / "settings.json"


def default_settings_file() -> Path:
    return app_root() / "config" / "settings.json"


def theme_dirs() -> list[Path]:
    """Theme search path, user themes taking precedence over shipped ones."""
    return [user_dir() / "themes", app_root() / "config" / "themes"]


def plugin_dirs() -> list[Path]:
    """Plugin search path. User plugins win on a name collision."""
    return [user_dir() / "plugins", app_root() / "plugins"]


def log_file() -> Path:
    logs = user_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "island.log"


def find_theme(name: str) -> Path | None:
    """Resolve a theme name (without extension) across the search path."""
    for directory in theme_dirs():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None
