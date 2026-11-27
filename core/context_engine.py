"""Decides what the island should say about the user's current context.

Inputs are the foreground window (event-driven) and the capability consent
store (also event-driven). Output is a single piece of content published
into the arbiter. Nothing here polls.

Profile precedence, highest first:

``meeting``
    The microphone is live. Detected from the consent store rather than
    from a list of conferencing apps, so Teams, Zoom, Discord, a Meet tab
    and whatever ships next year all work identically, including the case
    where the user is in a call while looking at a different window.
``developer``
    An editor or terminal is in front and its workspace resolves to a Git
    repository. Shows branch and working-tree state.
``browser``
    A browser is in front. The download watcher provides the useful part;
    the profile itself just labels the context.
``generic``
    Anything else. Deliberately quiet: the island stays out of the way
    rather than narrating every window the user touches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from core import git_probe
from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.model import IslandContent, Priority, Urgency
from core.osbridge.consent import MICROPHONE, ConsentWatcher
from core.osbridge.window_hooks import ForegroundInfo, ForegroundWatcher

PROFILE_MEETING = "meeting"
PROFILE_DEVELOPER = "developer"
PROFILE_BROWSER = "browser"
PROFILE_GENERIC = "generic"


def default_project_roots() -> list[Path]:
    """Reasonable places to look for the user's repositories."""
    home = Path.home()
    roots = [
        home / "Projects",
        home / "source" / "repos",
        home / "Documents" / "GitHub",
        home / "dev",
        home / "code",
    ]
    # Drive-root project folders are a common Windows habit.
    for drive in ("C:", "D:", "E:"):
        roots.append(Path(drive + "\\Projects"))
        roots.append(Path(drive + "\\dev"))
    return [r for r in roots if r.is_dir()]


class _GitSignals(QObject):
    done = Signal(object)  # RepoState


class _GitTask(QRunnable):
    """Reads Git state off the UI thread.

    ``git status`` on a cold cache can take hundreds of milliseconds. Doing
    that on the GUI thread would stall the island's animation every time
    the user alt-tabbed into an editor.
    """

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.signals = _GitSignals()
        # The signal object lives on this runnable, so letting Qt delete it
        # the moment run() returns can drop the queued result before the GUI
        # thread sees it. The engine holds the reference instead.
        self.setAutoDelete(False)

    def run(self) -> None:
        branch, detached = git_probe.read_branch(self.repo)
        dirty = git_probe.count_dirty(self.repo)
        self.signals.done.emit(
            git_probe.RepoState(
                path=self.repo, branch=branch, dirty=dirty, detached=detached
            )
        )


@dataclass
class ContextState:
    profile: str = PROFILE_GENERIC
    process: str = ""
    title: str = ""
    repo: git_probe.RepoState | None = None
    meeting_since: float | None = None
    microphone_apps: tuple[str, ...] = ()
    fullscreen: bool = False


class ContextEngine(QObject):
    """Turns OS signals into island content."""

    profile_changed = Signal(str)
    fullscreen_changed = Signal(bool)

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        foreground: ForegroundWatcher,
        consent: ConsentWatcher,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "context")
        self._settings = settings
        self._foreground = foreground
        self._consent = consent
        self._state = ContextState()
        self._roots = default_project_roots()
        self._repo_for_title: dict[str, Path | None] = {}
        self._pending_repo: Path | None = None
        # Keeps in-flight probes alive until their results arrive; see the
        # note on _GitTask.setAutoDelete.
        self._running: set[_GitTask] = set()

        foreground.changed.connect(self._on_foreground)
        consent.updated.connect(self._on_consent)

        # While a meeting is running the island shows a call timer, which is
        # the one place a periodic tick is genuinely required. It only runs
        # during a call.
        self._meeting_tick = QTimer(self)
        self._meeting_tick.setInterval(1000)
        self._meeting_tick.timeout.connect(self._republish)

        # Git state is refreshed on a slow tick while a developer window is
        # in front, so edits show up without the user switching windows.
        self._git_tick = QTimer(self)
        self._git_tick.setInterval(6000)
        self._git_tick.timeout.connect(self._refresh_git)

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        self._foreground.poll()
        self._recompute()

    def stop(self) -> None:
        self._meeting_tick.stop()
        self._git_tick.stop()
        self._handle.clear()

    def state(self) -> ContextState:
        return self._state

    # ---- inputs ------------------------------------------------------

    def _on_foreground(self, info: ForegroundInfo) -> None:
        was_fullscreen = self._state.fullscreen
        self._state.process = info.process
        self._state.title = info.title
        self._state.fullscreen = info.fullscreen
        if info.fullscreen != was_fullscreen:
            self.fullscreen_changed.emit(info.fullscreen)
        self._recompute(info)

    def _on_consent(self, snapshot: dict) -> None:
        mic_users = snapshot.get(MICROPHONE, [])
        names = tuple(u.name for u in mic_users)
        live = bool(names)
        if live and self._state.meeting_since is None:
            self._state.meeting_since = time.time()
        elif not live:
            self._state.meeting_since = None
        self._state.microphone_apps = names
        self._recompute()

    # ---- decision ----------------------------------------------------

    def _classify(self, info: ForegroundInfo | None) -> str:
        if (
            self._settings.get("context.meeting_from_microphone", True)
            and self._state.meeting_since is not None
        ):
            return PROFILE_MEETING
        if info is None:
            return PROFILE_GENERIC
        process = info.process.lower()
        if process in self._settings.lowered_list("context.developer_processes"):
            return PROFILE_DEVELOPER
        if process in self._settings.lowered_list("context.browser_processes"):
            return PROFILE_BROWSER
        return PROFILE_GENERIC

    def _recompute(self, info: ForegroundInfo | None = None) -> None:
        if info is None:
            info = self._foreground.current()
        profile = self._classify(info)
        changed = profile != self._state.profile
        self._state.profile = profile

        if changed:
            self.profile_changed.emit(profile)
            # Timers follow the profile so nothing ticks outside its state.
            self._meeting_tick.setInterval(1000)
            if profile == PROFILE_MEETING:
                self._meeting_tick.start()
            else:
                self._meeting_tick.stop()
            if profile == PROFILE_DEVELOPER:
                self._git_tick.start()
            else:
                self._git_tick.stop()
                self._state.repo = None

        if profile == PROFILE_DEVELOPER and info is not None:
            self._ensure_repo(info.title)

        self._republish()

    # ---- developer profile -------------------------------------------

    def _ensure_repo(self, title: str) -> None:
        if not self._settings.get("context.show_git_branch", True):
            return
        if title in self._repo_for_title:
            repo = self._repo_for_title[title]
        else:
            repo = git_probe.resolve_repo(title, self._roots)
            if len(self._repo_for_title) > 128:
                self._repo_for_title.clear()
            self._repo_for_title[title] = repo
        if repo is None:
            self._state.repo = None
            return
        if self._state.repo is not None and self._state.repo.path == repo:
            return
        self._pending_repo = repo
        self._start_git_task(repo)

    def _refresh_git(self) -> None:
        if self._state.profile != PROFILE_DEVELOPER:
            return
        repo = self._state.repo.path if self._state.repo else self._pending_repo
        if repo is not None:
            self._start_git_task(repo)

    def _start_git_task(self, repo: Path) -> None:
        task = _GitTask(repo)
        self._running.add(task)
        task.signals.done.connect(
            lambda state, t=task: self._on_git_done(state, t)
        )
        QThreadPool.globalInstance().start(task)

    def _on_git_done(
        self, state: git_probe.RepoState, task: _GitTask | None = None
    ) -> None:
        if task is not None:
            self._running.discard(task)
        # A slow probe can land after the user has moved on; drop it rather
        # than showing a branch for a project that is no longer in front.
        if self._state.profile != PROFILE_DEVELOPER:
            return
        self._state.repo = state
        self._republish()

    # ---- output ------------------------------------------------------

    def _republish(self) -> None:
        content = self._build()
        if content is None:
            self._handle.clear()
            return
        self._handle.publish_content(content)

    def _build(self) -> IslandContent | None:
        state = self._state
        if state.profile == PROFILE_MEETING:
            return self._build_meeting()
        if state.profile == PROFILE_DEVELOPER:
            return self._build_developer()
        if state.profile == PROFILE_BROWSER:
            return None  # the download watcher owns this profile's content
        return None

    def _build_meeting(self) -> IslandContent:
        since = self._state.meeting_since or time.time()
        elapsed = max(0, int(time.time() - since))
        clock = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        apps = ", ".join(self._state.microphone_apps[:2]) or "an application"
        return IslandContent(
            key="context.meeting",
            priority=Priority.CONTEXT,
            title=f"Microphone live · {clock}",
            detail=f"in use by {apps}",
            glyph="mic",
            urgency=Urgency.SUCCESS,
            collapsed=f"mic · {clock}",
        )

    def _build_developer(self) -> IslandContent | None:
        repo = self._state.repo
        if repo is None:
            return None
        label = git_probe.describe(repo)
        return IslandContent(
            key="context.developer",
            priority=Priority.CONTEXT,
            title=label,
            detail=repo.name,
            glyph="branch",
            urgency=Urgency.INFO if (repo.dirty or 0) else Urgency.NEUTRAL,
            collapsed=label,
        )
