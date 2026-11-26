"""Adaptive Dynamic Island for Windows -- application entry point.

Assembles the pieces and owns their lifetimes. The structure is deliberately
flat: providers publish into the arbiter, the arbiter drives the window, and
the tray drives the providers. Nothing talks directly to the window except
the arbiter, which is what keeps features from entangling with each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import paths  # noqa: E402
from core.arbiter import ContentArbiter  # noqa: E402
from core.config import Settings  # noqa: E402
from core.context_engine import ContextEngine  # noqa: E402
from core.island_window import IslandWindow  # noqa: E402
from core.osbridge.consent import ConsentWatcher  # noqa: E402
from core.osbridge.window_hooks import ForegroundWatcher  # noqa: E402
from core.single_instance import SingleInstance  # noqa: E402
from core.theme import load_theme  # noqa: E402
from core.tray import TrayController  # noqa: E402
from ipc.server import ActivityServer  # noqa: E402
from workers.ambient import AmbientProvider  # noqa: E402

APP_NAME = "Adaptive Dynamic Island"


class Application(QObject):
    """Wires the subsystems together and owns their lifetime."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = Settings.load()
        self.theme = self._load_theme()

        self.arbiter = ContentArbiter(self)
        self.window = IslandWindow(self.theme)
        self.arbiter.content_changed.connect(self._on_content)

        self.foreground = ForegroundWatcher(parent=self)
        self.consent = ConsentWatcher(self)
        self.context = ContextEngine(
            self.arbiter, self.settings, self.foreground, self.consent, self
        )
        self.ambient = AmbientProvider(self.arbiter, parent=self)
        self.activities = ActivityServer(self.arbiter, self.settings, self)

        self.tray: TrayController | None = None
        self._user_hidden = False
        self._suppressed_by_fullscreen = False

        self.context.fullscreen_changed.connect(self._on_fullscreen)
        self.window.files_dropped.connect(self._on_files_dropped)

        # Replacing or unplugging a monitor moves the built-in panel's
        # coordinates, so the island has to be re-anchored.
        screen_signals = QGuiApplication.instance()
        if screen_signals is not None:
            screen_signals.screenAdded.connect(lambda _s: self._reposition())
            screen_signals.screenRemoved.connect(lambda _s: self._reposition())
        primary = QGuiApplication.primaryScreen()
        if primary is not None:
            primary.geometryChanged.connect(lambda _r: self._reposition())

    # ---- startup -----------------------------------------------------

    def _load_theme(self):
        name = str(self.settings.get("theme", "midnight_oled"))
        return load_theme(paths.find_theme(name))

    def start(self) -> None:
        self.window.reposition(
            animate=False,
            preference=str(self.settings.get("display.preference", "internal")),
            horizontal_offset=int(self.settings.get("display.horizontal_offset", 0)),
        )

        if TrayController.available():
            self.tray = TrayController(self.settings, self.arbiter, self.theme, self)
            self.tray.quit_requested.connect(self.shutdown)
            self.tray.visibility_toggled.connect(self._on_visibility_toggled)
            self.tray.provider_toggled.connect(self._on_provider_toggled)
            self.tray.theme_selected.connect(self._on_theme_selected)
            self.tray.reposition_requested.connect(self._reposition)
            self.tray.diagnostics_requested.connect(self._show_diagnostics)
            self.tray.show()

        hooked = self.foreground.start()
        if not hooked:
            print("[main] window hooks unavailable; falling back to polling")
        self.consent.start()

        if self.settings.get("providers.clock", True):
            self.ambient.start()
        if self.settings.get("providers.context", True):
            self.context.start()
        if self.settings.get("providers.activities", True):
            if self.activities.start():
                print(f"[main] live activity API on 127.0.0.1:{self.activities.port}")

        # Nothing has published yet on a cold start, so prime the window
        # rather than leaving a blank strip for a second.
        QTimer.singleShot(0, lambda: self._on_content(self.arbiter.current()))

    # ---- arbiter -> window -------------------------------------------

    def _on_content(self, content) -> None:
        if self._user_hidden or self._suppressed_by_fullscreen:
            self.window.set_content(None)
            return
        self.window.set_content(content)
        if self.tray is not None:
            if content is None:
                self.tray.set_tooltip(APP_NAME)
            else:
                self.tray.set_tooltip(f"{APP_NAME} — {content.collapsed_text}")

    def _refresh_window(self) -> None:
        self._on_content(self.arbiter.current())

    # ---- events ------------------------------------------------------

    def _on_fullscreen(self, fullscreen: bool) -> None:
        if not self.settings.get("behaviour.hide_on_fullscreen", True):
            return
        self._suppressed_by_fullscreen = fullscreen
        self._refresh_window()

    def _on_visibility_toggled(self, visible: bool) -> None:
        self._user_hidden = not visible
        self._refresh_window()

    def _on_provider_toggled(self, key: str, enabled: bool) -> None:
        if key == "clock":
            self.ambient.start() if enabled else self.ambient.stop()
        elif key == "context":
            self.context.start() if enabled else self.context.stop()
        elif key == "activities":
            self.activities.start() if enabled else self.activities.stop()
        # Remaining providers are wired as they are implemented; the menu
        # already persists their setting so nothing is lost in the meantime.
        self._refresh_window()

    def _on_theme_selected(self, name: str) -> None:
        self.settings.set("theme", name)
        self.settings.save()
        self.theme = self._load_theme()
        self.window.apply_theme(self.theme)
        if self.tray is not None:
            self.tray.apply_theme(self.theme)
            self.tray.refresh_menu()
        self._reposition()

    def _reposition(self) -> None:
        self.window.reposition(
            animate=False,
            preference=str(self.settings.get("display.preference", "internal")),
            horizontal_offset=int(self.settings.get("display.horizontal_offset", 0)),
        )

    def _on_files_dropped(self, paths_dropped: list) -> None:
        # The Drop-to-Action pipeline lands here. Until it exists, say so
        # plainly rather than silently swallowing the drop.
        if self.tray is not None:
            count = len(paths_dropped)
            noun = "file" if count == 1 else "files"
            self.tray.notify(
                APP_NAME,
                f"{count} {noun} dropped. Drop actions are not wired up yet.",
            )

    # ---- diagnostics -------------------------------------------------

    def _diagnostics_text(self) -> str:
        placement = self.window.placement()
        from core.geometry import describe as describe_placement

        lines = [
            f"Version: development build",
            f"Settings: {self.settings.path}",
            f"Theme: {self.theme.name}"
            + (f" ({self.theme.source})" if self.theme.source else " (built-in)"),
            "",
            "Display",
            f"  {describe_placement(placement) if placement else 'not resolved'}",
            "",
            "OS integration",
            f"  foreground hooks: {'installed' if self.foreground.hooked else 'POLLING FALLBACK'}",
            f"  microphone in use: {self.consent.microphone_live()}",
            f"  live activity API: "
            + (f"127.0.0.1:{self.activities.port}" if self.activities.listening else "off"),
            f"  camera in use: {self.consent.webcam_live()}",
            "",
            "Context",
            f"  profile: {self.context.state().profile}",
            f"  process: {self.context.state().process or 'unknown'}",
        ]
        repo = self.context.state().repo
        if repo is not None:
            lines.append(f"  repository: {repo.path} ({repo.branch}, {repo.dirty} dirty)")

        lines += ["", "Content candidates (highest priority first)"]
        candidates = self.arbiter.candidates()
        if not candidates:
            lines.append("  none")
        for item in candidates:
            lines.append(f"  [{int(item.priority):3d}] {item.key}: {item.title}")
        return "\n".join(lines)

    def _show_diagnostics(self) -> None:
        box = QMessageBox()
        box.setWindowTitle(f"{APP_NAME} — diagnostics")
        box.setText("Runtime state")
        box.setDetailedText(self._diagnostics_text())
        box.setIcon(QMessageBox.Icon.Information)
        box.exec()

    # ---- shutdown ----------------------------------------------------

    def shutdown(self) -> None:
        self.activities.stop()
        self.ambient.stop()
        self.context.stop()
        self.consent.stop()
        self.foreground.stop()
        self.window.hide()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def main() -> int:
    lock = SingleInstance()
    if not lock.acquire():
        # A second launch is almost always a double-click on the shortcut.
        print(f"{APP_NAME} is already running.")
        return 0

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("AdaptiveIsland")
    # Closing the last window must not end the process: the island spends
    # most of its life as a tray-only application.
    app.setQuitOnLastWindowClosed(False)

    controller = Application(app)
    controller.start()
    try:
        return app.exec()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
