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

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from core import paths
from core.arbiter import ContentArbiter
from core.config import Settings
from core.context_engine import ContextEngine
from core.island_window import IslandWindow
from core.osbridge.consent import ConsentWatcher
from core.osbridge.window_hooks import ForegroundWatcher
from core.plugin_manager import PluginManager
from core.single_instance import SingleInstance
from core.theme import load_theme
from core.tray import TrayController
from ipc.server import ActivityServer
from workers.ambient import AmbientProvider
from workers.clipboard_guard import ClipboardGuard
from workers.dropzone import DropActionProvider
from workers.privacy_guard import PrivacyGuard
from workers.sentinel import SystemSentinel

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
        self.window.set_passive_click_through(
            bool(self.settings.get("behaviour.click_through_when_passive", False))
        )
        self.arbiter.content_changed.connect(self._on_content)

        self.foreground = ForegroundWatcher(parent=self)
        self.consent = ConsentWatcher(self)
        self.context = ContextEngine(
            self.arbiter, self.settings, self.foreground, self.consent, self
        )
        self.ambient = AmbientProvider(self.arbiter, parent=self)
        self.drops = DropActionProvider(self.arbiter, self)
        self.clipboard = ClipboardGuard(self.arbiter, self.settings, self)
        self.plugins = PluginManager(self.arbiter, self.settings, self)
        self.sentinel = SystemSentinel(self.arbiter, self.settings, self)
        self.privacy = PrivacyGuard(
            self.arbiter, self.settings, self.foreground, self.consent, self
        )
        self.activities = ActivityServer(self.arbiter, self.settings, self)

        self.tray: TrayController | None = None
        self._user_hidden = False
        self._suppressed_by_fullscreen = False

        self.context.fullscreen_changed.connect(self._on_fullscreen)
        self.window.files_dropped.connect(self._on_files_dropped)
        self.drops.completed.connect(self._on_drop_completed)
        self.clipboard.secret_detected.connect(self._on_secret_detected)
        self.plugins.plugin_failed.connect(self._on_plugin_failed)
        self.privacy.warning_raised.connect(self._on_privacy_warning)
        self.sentinel.alert_raised.connect(self._on_system_alert)

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
            self.tray.plugin_toggled.connect(self._on_plugin_toggled)
            self.tray.plugins_reload_requested.connect(self._on_plugins_reload)
            self.tray.set_plugin_source(self.plugins.all)
            self.tray.privacy_watch_toggled.connect(
                self.privacy.set_manual_watch
            )
            self.tray.show()

        hooked = self.foreground.start()
        if not hooked:
            print("[main] window hooks unavailable; falling back to polling")
        self.consent.start()

        if self.settings.get("providers.clock", True):
            self.ambient.start()
        if self.settings.get("providers.context", True):
            self.context.start()
        if self.settings.get("providers.clipboard", False):
            self.clipboard.start()
        if self.settings.get("providers.sentinel", False):
            self.sentinel.start()
        if self.settings.get("providers.privacy", False):
            self.privacy.start()
        if self.settings.get("providers.plugins", True):
            report = self.plugins.discover()
            self.plugins.start_enabled()
            print(f"[main] plugins: {report.summary()}")
        if self.settings.get("providers.activities", True) and (
            self.activities.start()
        ):
            print(
                "[main] live activity API on "
                f"127.0.0.1:{self.activities.port}"
            )

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

    def _start_plugins(self) -> None:
        self.plugins.discover()
        self.plugins.start_enabled()

    def _start_privacy(self) -> None:
        if not self.privacy.start() and self.tray is not None:
            self.tray.notify(
                APP_NAME, f"Privacy guard: {self.privacy.state().error}"
            )

    def _provider_switches(self) -> dict:
        """Map each toggleable provider to its start and stop calls.

        A table rather than a chain of branches. An earlier version of
        this method was an if/elif ladder, and an edit landed a line at
        the wrong indentation inside it: the result still parsed, still
        passed the tests, and quietly unloaded every plugin whenever any
        feature was toggled. A table has nowhere for a stray statement to
        hide.

        "drop" is absent on purpose: it is driven entirely by the window's
        drop events and reads its setting when one arrives.
        """
        return {
            "clock": (self.ambient.start, self.ambient.stop),
            "context": (self.context.start, self.context.stop),
            "activities": (self.activities.start, self.activities.stop),
            "clipboard": (self.clipboard.start, self.clipboard.stop),
            "sentinel": (self.sentinel.start, self.sentinel.stop),
            "privacy": (self._start_privacy, self.privacy.stop),
            "plugins": (self._start_plugins, self.plugins.unload_all),
        }

    def _on_provider_toggled(self, key: str, enabled: bool) -> None:
        switches = self._provider_switches().get(key)
        if switches is not None:
            start, stop = switches
            (start if enabled else stop)()
        self._refresh_window()

    def _on_plugin_toggled(self, plugin_id: str, enabled: bool) -> None:
        self.plugins.set_enabled(plugin_id, enabled)

    def _on_plugins_reload(self) -> None:
        report = self.plugins.reload()
        if self.tray is not None:
            self.tray.refresh_menu()
            message = report.summary()
            if report.rejected:
                first = report.rejected[0]
                message += f" — {first[0]}: {first[1]}"
            self.tray.notify(APP_NAME, f"Plugins rescanned: {message}")

    def _on_system_alert(self, _kind: str, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(APP_NAME, message)

    def _on_privacy_warning(self, kind: str) -> None:
        if self.tray is not None:
            label = (
                "Someone may be looking over your shoulder."
                if kind == "shoulder-surfing"
                else "You appear to have stepped away."
            )
            self.tray.notify(APP_NAME, label)

    def _on_plugin_failed(self, plugin_id: str, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(APP_NAME, f"Plugin {plugin_id} failed: {message}")

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
        if not self.settings.get("providers.drop", True):
            return
        self.drops.handle_drop([str(p) for p in paths_dropped])

    def _on_secret_detected(self, label: str) -> None:
        # No notification body: a toast is rendered by Windows and could
        # outlive the island's own warning, so it says what happened
        # without repeating anything sensitive.
        if self.tray is not None:
            self.tray.notify(
                APP_NAME, f"{label} detected on the clipboard and hidden."
            )

    def _on_drop_completed(self, summary: str) -> None:
        # The island's result state expires after a few seconds, so a tray
        # notification is what makes the outcome recoverable if the user
        # was looking elsewhere when it finished.
        if self.tray is not None:
            self.tray.notify(APP_NAME, summary)

    # ---- diagnostics -------------------------------------------------

    def _diagnostics_text(self) -> str:
        placement = self.window.placement()
        from core.geometry import describe as describe_placement

        lines = [
            "Version: development build",
            f"Settings: {self.settings.path}",
            f"Theme: {self.theme.name}"
            + (f" ({self.theme.source})" if self.theme.source else " (built-in)"),
            "",
            "Display",
            f"  {describe_placement(placement) if placement else 'not resolved'}",
            "",
            "OS integration",
            "  foreground hooks: "
            + ("installed" if self.foreground.hooked else "POLLING FALLBACK"),
            f"  microphone in use: {self.consent.microphone_live()}",
            "  live activity API: "
            + (
                f"127.0.0.1:{self.activities.port}"
                if self.activities.listening
                else "off"
            ),
            f"  camera in use: {self.consent.webcam_live()}",
            "",
            "Context",
            f"  profile: {self.context.state().profile}",
            f"  process: {self.context.state().process or 'unknown'}",
        ]
        repo = self.context.state().repo
        if repo is not None:
            lines.append(
                f"  repository: {repo.path} ({repo.branch}, {repo.dirty} dirty)"
            )

        lines += ["", "System sentinel", self.sentinel.describe()]
        lines += ["", "Privacy guard", self.privacy.describe()]
        lines += ["", "Plugins", self.plugins.describe()]
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
        # Order matters: anything owning a thread or a device goes first,
        # so the camera is released and the worker joined before Qt starts
        # tearing objects down underneath them.
        self.privacy.stop()
        self.sentinel.stop()
        self.plugins.unload_all()
        self.activities.stop()
        self.clipboard.stop()
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
