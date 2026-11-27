"""System tray icon, menu and diagnostics.

The tray is the only conventional UI the app has, so it carries two jobs
beyond on/off switches:

* **Honesty about what is running.** The diagnostics entry reports which
  screen the island anchored to, whether the OS hooks actually installed,
  and what the island is showing right now. A background overlay that
  cannot explain itself is impossible to support.
* **A visible privacy switch.** Anything touching the camera is off by
  default and toggled here, never buried in a settings file.

The icon is drawn at runtime from the same vector glyph set the island
uses, so there is no .ico asset to keep in sync and it stays crisp at every
tray scaling factor.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from core import paths
from core.arbiter import ContentArbiter
from core.config import Settings
from core.theme import Theme

_PROVIDER_LABELS = {
    "clock": "Clock",
    "context": "Context profiles",
    "activities": "Live activities",
    "drop": "Drop to action (OCR, compress)",
    "plugins": "Plugins",
    "clipboard": "Clipboard stack",
    "sentinel": "System alerts",
    "privacy": "Privacy guard (camera)",
}


def build_icon(theme: Theme, size: int = 64) -> QIcon:
    """Draw the island's own silhouette as the tray icon."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    pill = QRectF(size * 0.10, size * 0.34, size * 0.80, size * 0.32)
    radius = pill.height() / 2.0
    path = QPainterPath()
    path.addRoundedRect(pill, radius, radius)
    painter.fillPath(path, QColor(236, 238, 242))

    # A single accent dot, the way the island shows a live activity.
    dot = theme.color("accent_neutral")
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(dot)
    painter.drawEllipse(
        QRectF(
            pill.right() - pill.height() * 0.78,
            pill.top() + pill.height() * 0.28,
            pill.height() * 0.44,
            pill.height() * 0.44,
        )
    )
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    """Owns the tray icon and translates menu clicks into signals."""

    quit_requested = Signal()
    plugin_toggled = Signal(str, bool)
    plugins_reload_requested = Signal()
    visibility_toggled = Signal(bool)
    provider_toggled = Signal(str, bool)
    theme_selected = Signal(str)
    reposition_requested = Signal()
    diagnostics_requested = Signal()

    def __init__(
        self,
        settings: Settings,
        arbiter: ContentArbiter,
        theme: Theme,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._arbiter = arbiter
        self._theme = theme
        self._visible = True
        self._plugin_source = None

        self._tray = QSystemTrayIcon(build_icon(theme), self)
        self._tray.setToolTip("Adaptive Dynamic Island")
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    # ---- menu --------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self._menu
        menu.clear()

        self._visibility_action = QAction("Hide island", menu)
        self._visibility_action.triggered.connect(self._toggle_visibility)
        menu.addAction(self._visibility_action)

        menu.addSeparator()

        features = menu.addMenu("Features")
        for key, label in _PROVIDER_LABELS.items():
            action = QAction(label, features)
            action.setCheckable(True)
            action.setChecked(bool(self._settings.get(f"providers.{key}", False)))
            action.toggled.connect(
                lambda checked, k=key: self._on_provider_toggled(k, checked)
            )
            features.addAction(action)

        self._build_plugin_menu(menu)

        themes = menu.addMenu("Theme")
        seen: set[str] = set()
        for directory in paths.theme_dirs():
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.stem in seen:
                    continue
                seen.add(path.stem)
                action = QAction(path.stem.replace("_", " "), themes)
                action.setCheckable(True)
                action.setChecked(self._settings.get("theme") == path.stem)
                action.triggered.connect(
                    lambda _checked=False, name=path.stem: (
                        self.theme_selected.emit(name)
                    )
                )
                themes.addAction(action)
        if not seen:
            empty = QAction("No themes found", themes)
            empty.setEnabled(False)
            themes.addAction(empty)

        menu.addSeparator()

        recenter = QAction("Re-centre on this display", menu)
        recenter.triggered.connect(self.reposition_requested.emit)
        menu.addAction(recenter)

        diagnostics = QAction("Diagnostics…", menu)
        diagnostics.triggered.connect(self.diagnostics_requested.emit)
        menu.addAction(diagnostics)

        open_settings = QAction("Open settings folder", menu)
        open_settings.triggered.connect(self._open_settings_folder)
        menu.addAction(open_settings)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

    def _build_plugin_menu(self, menu: QMenu) -> None:
        """List discovered plugins with a checkbox each."""
        plugins = menu.addMenu("Plugins")
        entries = self._plugin_entries() if self._plugin_source else []
        for entry in entries:
            action = QAction(entry.name, plugins)
            action.setCheckable(True)
            action.setChecked(entry.active)
            if not entry.healthy:
                action.setEnabled(False)
                action.setText(f"{entry.name} — {entry.disabled_reason}")
            action.setToolTip(entry.description or entry.id)
            action.toggled.connect(
                lambda checked, pid=entry.id: self.plugin_toggled.emit(
                    pid, checked
                )
            )
            plugins.addAction(action)
        if not entries:
            empty = QAction("No plugins found", plugins)
            empty.setEnabled(False)
            plugins.addAction(empty)
        plugins.addSeparator()
        rescan = QAction("Rescan plugins folder", plugins)
        rescan.triggered.connect(self.plugins_reload_requested.emit)
        plugins.addAction(rescan)
        open_folder = QAction("Open plugins folder", plugins)
        open_folder.triggered.connect(self._open_plugins_folder)
        plugins.addAction(open_folder)

    def _plugin_entries(self):
        try:
            return self._plugin_source()
        except Exception as exc:  # the menu must still open
            print(f"[tray] could not list plugins: {exc}")
            return []

    def set_plugin_source(self, source) -> None:
        """Give the tray a callable returning the loaded plugin list."""
        self._plugin_source = source
        self.refresh_menu()

    @staticmethod
    def _open_plugins_folder() -> None:
        import os

        folder = paths.user_dir() / "plugins"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def refresh_menu(self) -> None:
        self._build_menu()
        self._tray.setContextMenu(self._menu)

    # ---- actions -----------------------------------------------------

    def _toggle_visibility(self) -> None:
        self._visible = not self._visible
        self._visibility_action.setText(
            "Hide island" if self._visible else "Show island"
        )
        self.visibility_toggled.emit(self._visible)

    def _on_provider_toggled(self, key: str, checked: bool) -> None:
        self._settings.set(f"providers.{key}", checked)
        self._settings.save()
        self.provider_toggled.emit(key, checked)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _open_settings_folder(self) -> None:
        import os

        os.startfile(str(paths.user_dir()))

    # ---- presentation -------------------------------------------------

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._tray.setIcon(build_icon(theme))

    def notify(self, title: str, message: str) -> None:
        self._tray.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, 4000
        )

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_tooltip(self, text: str) -> None:
        # Windows truncates long tooltips silently; keep it short.
        self._tray.setToolTip(text[:120])

    def quit_app(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()
