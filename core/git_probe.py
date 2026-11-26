r"""Work out which repository the user is looking at, and its state.

The hard part is that a window title is all we get. An editor does not tell
the outside world which folder it has open, so the repository has to be
inferred:

1. If the title contains an absolute path, use it directly. Terminals
   usually do this.
2. Otherwise take the workspace name out of the title. VS Code's default
   title is ``file.py - project-name - Visual Studio Code``, so the segment
   before the application name is the workspace, and the same shape holds
   for the JetBrains IDEs and Sublime.
3. Look that name up under the configured project roots.

Branch comes from reading ``.git/HEAD``, which is a single small file and
needs no subprocess. The dirty-file count does need Git, so it runs off the
UI thread and is cached: asking the island for a branch must never make a
keystroke wait.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Trailing application names to strip out of a window title.
_APP_SUFFIXES = (
    "visual studio code",
    "visual studio",
    "cursor",
    "windsurf",
    "sublime text",
    "pycharm",
    "intellij idea",
    "webstorm",
    "clion",
    "goland",
    "rider",
    "android studio",
    "neovim",
    "vim",
)

_PATH_RE = re.compile(r"[A-Za-z]:\\[^\"<>|?*\r\n]+")
# Editors mark unsaved changes with a leading bullet or asterisk.
_DIRTY_MARK = re.compile(r"^\s*[●•*]\s*")

_CACHE_TTL = 5.0
_status_cache: dict[str, tuple[float, int | None]] = {}


@dataclass(frozen=True)
class RepoState:
    path: Path
    branch: str | None
    # None when Git is unavailable or the count has not been computed yet.
    dirty: int | None
    detached: bool = False

    @property
    def name(self) -> str:
        return self.path.name


def candidate_names(title: str) -> list[str]:
    """Workspace names a title might be referring to, best guess first."""
    if not title:
        return []
    cleaned = _DIRTY_MARK.sub("", title).strip()
    parts = [p.strip() for p in re.split(r"\s[-—–]\s", cleaned) if p.strip()]
    if not parts:
        return []
    # Drop a trailing application name, then prefer the last remaining
    # segment: that is where every editor puts the workspace.
    if len(parts) > 1 and parts[-1].lower() in _APP_SUFFIXES:
        parts = parts[:-1]
    out: list[str] = []
    for part in reversed(parts):
        # "akmal@host: ~/project" and "project [SSH]" both appear in the wild.
        part = part.split(":")[-1].strip()
        part = re.sub(r"\s*\[[^\]]*\]\s*$", "", part).strip()
        part = part.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
        if part and part not in out and len(part) < 80:
            out.append(part)
    return out


def path_in_title(title: str) -> Path | None:
    """An absolute Windows path mentioned in the title, if any."""
    match = _PATH_RE.search(title or "")
    if not match:
        return None
    candidate = Path(match.group(0).rstrip(" -"))
    if candidate.is_file():
        candidate = candidate.parent
    return candidate if candidate.is_dir() else None


def find_repo_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.git`` entry."""
    try:
        current = start.resolve()
    except OSError:
        return None
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return None


def resolve_repo(title: str, roots: list[Path]) -> Path | None:
    """Best-effort repository for the given window title."""
    direct = path_in_title(title)
    if direct is not None:
        found = find_repo_root(direct)
        if found is not None:
            return found

    names = candidate_names(title)
    if not names:
        return None
    for name in names:
        for root in roots:
            candidate = root / name
            try:
                if (candidate / ".git").exists():
                    return candidate
            except OSError:
                continue
    return None


def read_branch(repo: Path) -> tuple[str | None, bool]:
    """Branch name from ``.git/HEAD``; second value is True when detached.

    Handles worktrees and submodules, where ``.git`` is a file pointing at
    the real git directory rather than being the directory itself.
    """
    git_path = repo / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_dir = Path(pointer.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (repo / git_dir).resolve()
            else:
                return None, False
        else:
            git_dir = git_path
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None, False

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        return ref.rsplit("/", 1)[-1], False
    # A bare hash means a detached HEAD.
    return (head[:7] or None), True


def count_dirty(repo: Path, timeout: float = 2.5) -> int | None:
    """Number of changed files, cached briefly. ``None`` when Git is absent.

    Blocking. Call it from a worker thread, never from the UI thread.
    """
    key = str(repo)
    now = time.monotonic()
    cached = _status_cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]

    value: int | None = None
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode == 0:
            value = sum(1 for line in result.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        value = None

    _status_cache[key] = (now, value)
    return value


def describe(state: RepoState) -> str:
    """Short label for the collapsed pill."""
    branch = state.branch or "no branch"
    if state.dirty is None:
        return branch
    if state.dirty == 0:
        return f"{branch} · clean"
    noun = "change" if state.dirty == 1 else "changes"
    return f"{branch} · {state.dirty} {noun}"
