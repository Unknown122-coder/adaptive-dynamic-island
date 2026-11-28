"""The control panel that drops down from the island.

Settings used to live entirely in the tray menu, which was wrong for a
reason that only shows up in use: a Windows menu closes the moment you
click an item, so turning on four features meant opening the menu four
times. A menu is right for commands and wrong for a board of switches.

Putting the switches in the pill itself was the other obvious idea and is
worse. The expanded island is around 400 by 90 pixels, so a grid of
toggles in there would mean unlabelled targets a few pixels tall fighting
the content the island exists to show.

So this is a panel anchored directly beneath the island, in the shape
Windows quick settings and macOS Control Center both use. It stays open
until dismissed, so four switches take four clicks. It is a ``Qt.Popup``,
which is what gives it close-on-click-outside and Escape for free, and
keeps it from taking focus away from whatever the user was typing in.

Everything is painted rather than styled through a stylesheet, for the
same reason the island is: the shapes are custom, the palette comes from
the theme, and a stylesheet would be a second place for colours to drift.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.theme import Theme

PANEL_WIDTH = 440
PANEL_PADDING = 18
PANEL_RADIUS = 18
# Clearance between the island's lower edge and the panel's upper edge.
ANCHOR_GAP = 8

FEATURE_ROWS: tuple[tuple[str, str], ...] = (
    ("clock", "Clock"),
    ("activities", "Live activities"),
    ("context", "Context profiles"),
    ("drop", "Drop to action"),
    ("clipboard", "Clipboard guard"),
    ("sentinel", "System alerts"),
    ("privacy", "Privacy guard (camera)"),
    ("plugins", "Plugins"),
)


# Words that should stay upper-case when a theme filename is prettified.
# An explicit list, because every rule-of-thumb for this (short words,
# no vowels, all consonants) turns "nord" into "NORD" and "high" into
# "HIGH" the moment someone adds a theme.
_ACRONYMS = frozenset({"oled", "rgb", "hdr", "lcd", "ui", "hc", "amoled"})


def prettify_theme_name(stem: str) -> str:
    """``midnight_oled`` becomes ``Midnight OLED``."""
    words = [w for w in stem.replace("-", "_").split("_") if w]
    return " ".join(
        word.upper() if word.lower() in _ACRONYMS else word.capitalize()
        for word in words
    )


class _Switch(QWidget):
    """A labelled on/off switch, drawn to match the island."""

    toggled = Signal(bool)

    def __init__(
        self,
        label: str,
        checked: bool,
        theme: Theme,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._subtitle = subtitle
        self._checked = bool(checked)
        self._theme = theme
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)

    # ---- state -------------------------------------------------------

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool, notify: bool = False) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.update()
        if notify:
            self.toggled.emit(checked)

    def sizeHint(self) -> QSize:
        return QSize(190, 34)

    # ---- interaction ---------------------------------------------------

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.set_checked(not self._checked, notify=True)

    # ---- painting ------------------------------------------------------

    def paintEvent(self, event) -> None:
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )

        if self._hovered:
            hover = QColor(theme.color("border"))
            hover.setAlpha(28)
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()).adjusted(-4, 0, 4, 0), 8, 8)
            painter.fillPath(path, hover)

        track_w, track_h = 34.0, 18.0
        track = QRectF(
            0.0, (self.height() - track_h) / 2.0, track_w, track_h
        )
        radius = track_h / 2.0
        track_path = QPainterPath()
        track_path.addRoundedRect(track, radius, radius)
        if self._checked:
            painter.fillPath(track_path, theme.color("accent_success"))
        else:
            painter.fillPath(track_path, theme.color("progress_track"))
            pen = QPen(theme.color("border"))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawPath(track_path)

        knob_r = radius - 3.0
        knob_x = (
            track.right() - radius if self._checked else track.left() + radius
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 240 if self._checked else 170))
        painter.drawEllipse(QPoint(int(knob_x), int(track.center().y())),
                            int(knob_r), int(knob_r))

        text_left = track_w + 12.0
        font = QFont("Segoe UI Variable Text", 10)
        painter.setFont(font)
        painter.setPen(
            theme.color("text") if self._checked else theme.color("text_dim")
        )
        metrics = QFontMetrics(font)
        available = int(self.width() - text_left)
        painter.drawText(
            QRectF(text_left, 0, self.width() - text_left, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(
                self._label, Qt.TextElideMode.ElideRight, max(available, 1)
            ),
        )
        painter.end()


class _Chip(QWidget):
    """A small clickable pill, used for themes and commands."""

    clicked = Signal()

    def __init__(
        self,
        label: str,
        theme: Theme,
        selected: bool = False,
        danger: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._theme = theme
        self._selected = selected
        self._danger = danger
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(26)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(QFont("Segoe UI Variable Text", 10))
        return QSize(metrics.horizontalAdvance(self._label) + 26, 26)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        if self._selected:
            fill = QColor(theme.color("accent_neutral"))
            fill.setAlpha(210)
        elif self._danger and self._hovered:
            fill = QColor(theme.color("accent_critical"))
            fill.setAlpha(200)
        else:
            fill = QColor(theme.color("border"))
            fill.setAlpha(90 if self._hovered else 50)
        painter.fillPath(path, fill)

        painter.setFont(QFont("Segoe UI Variable Text", 10))
        painter.setPen(
            theme.color("text") if (self._selected or self._hovered)
            else theme.color("text_dim")
        )
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)
        painter.end()


class _SectionLabel(QLabel):
    def __init__(self, text: str, theme: Theme) -> None:
        super().__init__(text.upper())
        font = QFont("Segoe UI Variable Text", 8)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        self.setFont(font)
        colour = theme.color("text_dim")
        self.setStyleSheet(f"color: rgba({colour.red()},{colour.green()},"
                           f"{colour.blue()},{colour.alpha()});")


class ControlPanel(QWidget):
    """Anchored panel holding every switch the island has."""

    feature_toggled = Signal(str, bool)
    plugin_toggled = Signal(str, bool)
    theme_selected = Signal(str)
    diagnostics_requested = Signal()
    quit_requested = Signal()
    closed = Signal()

    def __init__(
        self,
        theme: Theme,
        is_feature_on: Callable[[str], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._is_feature_on = is_feature_on
        self._feature_switches: dict[str, _Switch] = {}
        self._plugin_switches: dict[str, _Switch] = {}
        self._theme_chips: dict[str, _Chip] = {}

        # Qt.Popup is what makes this behave: it closes on an outside
        # click or Escape without any code, and it does not take focus
        # away from whatever the user was typing in.
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(PANEL_WIDTH)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(
            PANEL_PADDING, PANEL_PADDING, PANEL_PADDING, PANEL_PADDING
        )
        self._root.setSpacing(6)

        self._features_grid = QGridLayout()
        self._features_grid.setHorizontalSpacing(18)
        self._features_grid.setVerticalSpacing(2)
        self._plugins_grid = QGridLayout()
        self._plugins_grid.setHorizontalSpacing(18)
        self._plugins_grid.setVerticalSpacing(2)

        self._root.addWidget(_SectionLabel("Features", theme))
        self._root.addLayout(self._features_grid)
        self._plugins_label = _SectionLabel("Plugins", theme)
        self._root.addSpacing(8)
        self._root.addWidget(self._plugins_label)
        self._root.addLayout(self._plugins_grid)

        self._root.addSpacing(10)
        self._footer = QHBoxLayout()
        self._footer.setSpacing(6)
        self._root.addLayout(self._footer)

        self._build_features()

    # ---- construction ---------------------------------------------------

    def _build_features(self) -> None:
        for index, (key, label) in enumerate(FEATURE_ROWS):
            switch = _Switch(label, self._is_feature_on(key), self._theme)
            switch.toggled.connect(
                lambda checked, k=key: self.feature_toggled.emit(k, checked)
            )
            self._feature_switches[key] = switch
            self._features_grid.addWidget(switch, index // 2, index % 2)

    def set_plugins(self, entries: list) -> None:
        """Rebuild the plugin switches from the manager's list."""
        while self._plugins_grid.count():
            item = self._plugins_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._plugin_switches.clear()

        if not entries:
            empty = QLabel("No plugins found")
            colour = self._theme.color("text_dim")
            empty.setStyleSheet(
                f"color: rgba({colour.red()},{colour.green()},"
                f"{colour.blue()},{colour.alpha()});"
            )
            empty.setFont(QFont("Segoe UI Variable Text", 10))
            self._plugins_grid.addWidget(empty, 0, 0, 1, 2)
            return

        for index, entry in enumerate(entries):
            label = entry.name
            if not entry.healthy:
                label = f"{entry.name} (disabled)"
            switch = _Switch(label, entry.active, self._theme)
            switch.setEnabled(entry.healthy)
            switch.toggled.connect(
                lambda checked, pid=entry.id: self.plugin_toggled.emit(
                    pid, checked
                )
            )
            self._plugin_switches[entry.id] = switch
            self._plugins_grid.addWidget(switch, index // 2, index % 2)

    def set_themes(self, names: list[str], current: str) -> None:
        while self._footer.count():
            item = self._footer.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._theme_chips.clear()

        for name in names:
            chip = _Chip(
                prettify_theme_name(name),
                self._theme,
                selected=(name == current),
            )
            chip.clicked.connect(
                lambda n=name: self.theme_selected.emit(n)
            )
            self._theme_chips[name] = chip
            self._footer.addWidget(chip)

        self._footer.addStretch(1)
        diagnostics = _Chip("Diagnostics", self._theme)
        diagnostics.clicked.connect(self._emit_diagnostics)
        self._footer.addWidget(diagnostics)
        quit_chip = _Chip("Quit", self._theme, danger=True)
        quit_chip.clicked.connect(self._emit_quit)
        self._footer.addWidget(quit_chip)

    def _emit_diagnostics(self) -> None:
        # Close first: a modal dialog opening behind a popup is unreachable.
        self.hide()
        self.diagnostics_requested.emit()

    def _emit_quit(self) -> None:
        self.hide()
        self.quit_requested.emit()

    # ---- state ----------------------------------------------------------

    def sync(self) -> None:
        """Re-read every switch from the live settings."""
        for key, switch in self._feature_switches.items():
            switch.set_checked(self._is_feature_on(key))

    def set_feature(self, key: str, enabled: bool) -> None:
        switch = self._feature_switches.get(key)
        if switch is not None:
            switch.set_checked(enabled)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (_Switch, _Chip)):
                widget._theme = theme  # noqa: SLF001 - sibling widget
        self.update()

    # ---- presentation ----------------------------------------------------

    def show_under(self, island_rect: QRect, screen_rect: QRect) -> None:
        """Open centred beneath the island, clamped onto the screen."""
        self.adjustSize()
        width = self.width()
        height = self.height()
        x = island_rect.center().x() - width // 2
        y = island_rect.bottom() + ANCHOR_GAP

        margin = 8
        x = max(
            screen_rect.left() + margin,
            min(x, screen_rect.right() - width - margin + 1),
        )
        y = min(y, screen_rect.bottom() - height - margin + 1)
        self.move(x, y)
        self.show()
        self.raise_()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()

    def paintEvent(self, event) -> None:
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)
        surface = QColor(theme.color("surface"))
        surface.setAlpha(min(255, surface.alpha() + 12))
        painter.fillPath(path, surface)
        pen = QPen(theme.color("border"))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()
