"""The island: a frameless, non-activating overlay that morphs between a
collapsed pill and an expanded card.

Design notes that matter for the result rather than the code:

* **One input, one animation path.** The window renders whatever
  :class:`~core.model.IslandContent` the arbiter hands it. It has no idea
  what Git or a webcam is, so adding a feature never touches the motion
  logic, and the motion stays coherent instead of each feature animating
  its own way.
* **Everything is painted, nothing is laid out.** A single ``paintEvent``
  beats a tree of child widgets here: the island is small, redraws often
  during animation, and needs pixel control over a rounded translucent
  shape. It also keeps the widget count, and therefore idle memory, flat.
* **No timer runs unless it has to.** The shimmer animation exists only
  while indeterminate progress is on screen. At rest the window schedules
  nothing at all, which is what makes a sub-1% idle CPU figure reachable.
* **Hover intent, not hover.** Opening on raw ``enterEvent`` makes the
  island twitch every time the cursor crosses the top of the screen on its
  way to a browser tab. Both directions are delayed.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from core import geometry, glyphs
from core.model import Action, IslandContent, Urgency
from core.osbridge import window_style
from core.theme import Theme

# Room reserved around the pill for its drop shadow. The island sits flush
# with the top edge, so no padding is needed above it. Keep this larger
# than the theme's shadow_blur or the shadow is clipped into a hard box.
SHADOW_PAD = 22


@dataclass
class _Hit:
    rect: QRect
    action: Action


class IslandWindow(QWidget):
    """The overlay window."""

    files_dropped = Signal(list)       # list[str] of local paths
    surface_clicked = Signal()
    expansion_changed = Signal(bool)

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._content: IslandContent | None = None
        self._pending: IslandContent | None = None
        self._expanded = False
        self._hovered = False
        self._drag_active = False
        self._content_opacity = 1.0
        self._shimmer = 0.0
        self._hits: list[_Hit] = []
        self._armed_destructive: Action | None = None
        self._placement: geometry.Placement | None = None
        self._pill_rect = QRect()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setWindowTitle("Adaptive Dynamic Island")

        # -- animations ------------------------------------------------
        self._morph = QPropertyAnimation(self, b"geometry", self)
        self._morph.setDuration(int(theme.motion("morph_ms")))
        self._morph.setEasingCurve(theme.morph_curve())

        self._fade = QPropertyAnimation(self, b"contentOpacity", self)
        self._fade.setDuration(int(theme.motion("fade_ms")))
        self._fade.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutQuad))
        self._fade.finished.connect(self._on_fade_finished)

        self._shimmer_anim = QPropertyAnimation(self, b"shimmer", self)
        self._shimmer_anim.setDuration(1200)
        self._shimmer_anim.setStartValue(0.0)
        self._shimmer_anim.setEndValue(1.0)
        self._shimmer_anim.setLoopCount(-1)

        # -- hover intent ----------------------------------------------
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.setInterval(int(theme.motion("hover_open_delay_ms")))
        self._open_timer.timeout.connect(lambda: self.set_expanded(True))

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(int(theme.motion("hover_close_delay_ms")))
        self._close_timer.timeout.connect(self._close_if_unhovered)

        # Windows demotes topmost windows now and then; re-assert cheaply.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(5000)
        self._topmost_timer.timeout.connect(self._reassert)
        self._topmost_timer.start()

        self._fonts_built = False
        self._f_title = QFont()
        self._f_detail = QFont()

    # ---- Qt properties used by the animations ------------------------

    def _get_content_opacity(self) -> float:
        return self._content_opacity

    def _set_content_opacity(self, value: float) -> None:
        self._content_opacity = max(0.0, min(1.0, float(value)))
        self.update()

    contentOpacity = Property(
        float, _get_content_opacity, _set_content_opacity
    )

    def _get_shimmer(self) -> float:
        return self._shimmer

    def _set_shimmer(self, value: float) -> None:
        self._shimmer = float(value)
        self.update()

    shimmer = Property(float, _get_shimmer, _set_shimmer)

    # ---- public API --------------------------------------------------

    @property
    def theme(self) -> Theme:
        return self._theme

    def apply_theme(self, theme: Theme) -> None:
        """Swap the theme at runtime, re-deriving metrics and motion."""
        self._theme = theme
        self._fonts_built = False
        self._morph.setDuration(int(theme.motion("morph_ms")))
        self._morph.setEasingCurve(theme.morph_curve())
        self._fade.setDuration(int(theme.motion("fade_ms")))
        self._open_timer.setInterval(int(theme.motion("hover_open_delay_ms")))
        self._close_timer.setInterval(int(theme.motion("hover_close_delay_ms")))
        self.reposition(animate=False)
        self.update()

    def reposition(self, animate: bool = True, preference: str = "internal",
                   horizontal_offset: int = 0) -> None:
        """Recompute placement, for example after a monitor is plugged in."""
        w, h = self._target_pill_size()
        pill, placement = geometry.resolve(
            w,
            h,
            top_inset=self._theme.metric("top_inset"),
            horizontal_offset=horizontal_offset,
            preference=preference,
        )
        self._placement = placement
        self._apply_pill_rect(pill, animate=animate)

    def placement(self) -> geometry.Placement | None:
        return self._placement

    def set_content(
        self, content: IslandContent | None, animate: bool = True
    ) -> None:
        """Render ``content``, cross-fading when it is a different item.

        ``animate=False`` swaps immediately with no fade. Preview rendering
        and tests need that: a cross-fade half-finished at grab time paints
        a nearly invisible island, which looks like a painting bug.
        """
        if content is None:
            self._content = None
            self._pending = None
            self._stop_shimmer()
            self.hide()
            return

        was_hidden = not self.isVisible()
        if not animate:
            self._fade.stop()
            self._content = content
            self._pending = None
            self._content_opacity = 1.0
            self._sync_shimmer()
            self._resize_for_content(animate=False)
            if was_hidden:
                self.show()
            self._refresh_interactivity()
            self.update()
            return

        if was_hidden:
            self._content = content
            self._content_opacity = 1.0
            self._sync_shimmer()
            self._resize_for_content(animate=False)
            self.show()
            self._reassert()
            self.update()
        elif content.replaces(self._content):
            # Different item: fade out, swap, fade back in.
            self._pending = content
            self._fade.stop()
            self._fade.setStartValue(self._content_opacity)
            self._fade.setEndValue(0.0)
            self._fade.start()
        else:
            # Same item updating in place: no entrance animation, just a
            # repaint, so a progress bar ticking does not strobe.
            self._content = content
            self._sync_shimmer()
            self._resize_for_content(animate=True)
            self.update()

        if content.auto_expand and not self._expanded:
            self.set_expanded(True)
        self._refresh_interactivity()

    def content(self) -> IslandContent | None:
        return self._content

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._armed_destructive = None
        self._resize_for_content(animate=True)
        self._refresh_interactivity()
        self.expansion_changed.emit(expanded)
        self.update()

    def is_expanded(self) -> bool:
        return self._expanded

    # ---- sizing ------------------------------------------------------

    def _expanded_height_for(self, content: IslandContent | None) -> int:
        """Size the card to its content instead of to a fixed rectangle.

        A fixed height leaves a band of dead space under anything without
        actions, which reads as a layout bug rather than as a design.
        """
        t = self._theme
        height = 2 * t.metric("padding_v") + 20  # title row
        if content is not None:
            if content.detail:
                height += 18
            if content.progress is not None:
                height += t.metric("progress_height") + 12
            if content.actions:
                height += 22 + 8
        return max(height, t.metric("collapsed_height") + 18)

    def _target_pill_size(self) -> tuple[int, int]:
        t = self._theme
        if self._expanded or self._drag_active:
            return (
                t.metric("expanded_width"),
                self._expanded_height_for(
                    None if self._drag_active else self._content
                ),
            )
        width = t.metric("collapsed_width")
        content = self._content
        if content is not None:
            # Let the collapsed pill breathe for longer labels rather than
            # eliding aggressively, but keep a hard ceiling so it never
            # grows into a banner.
            self._ensure_fonts()
            fm = QFontMetrics(self._f_title)
            needed = (
                fm.horizontalAdvance(content.collapsed_text)
                + 2 * t.metric("padding_h")
                + (t.metric("glyph_size") + 7 if content.glyph else 0)
                # Metrics measured without a paint device run a little
                # short of what the painter actually lays down, and being
                # two pixels short elides a label that would have fitted.
                + 8
            )
            ceiling = max(
                t.metric("collapsed_width"),
                int(t.metric("expanded_width") * 0.86),
            )
            width = max(width, min(needed, ceiling))
        return width, t.metric("collapsed_height")

    def _resize_for_content(self, animate: bool) -> None:
        if self._placement is None:
            self.reposition(animate=False)
            return
        w, h = self._target_pill_size()
        pill = geometry.rect_for_size(self._placement, w, h)
        self._apply_pill_rect(pill, animate=animate)

    def _window_rect_for(self, pill: QRect) -> QRect:
        """Grow the pill rect to leave room for the shadow."""
        return QRect(
            pill.left() - SHADOW_PAD,
            pill.top(),
            pill.width() + 2 * SHADOW_PAD,
            pill.height() + SHADOW_PAD,
        )

    def _apply_pill_rect(self, pill: QRect, animate: bool) -> None:
        self._pill_rect = QRect(
            SHADOW_PAD, 0, pill.width(), pill.height()
        )
        target = self._window_rect_for(pill)
        if animate and self.isVisible():
            self._morph.stop()
            self._morph.setStartValue(self.geometry())
            self._morph.setEndValue(target)
            self._morph.start()
        else:
            self._morph.stop()
            self.setGeometry(target)
        self.update()

    # ---- animation plumbing ------------------------------------------

    def _on_fade_finished(self) -> None:
        if self._pending is not None and self._content_opacity <= 0.01:
            self._content = self._pending
            self._pending = None
            self._sync_shimmer()
            self._resize_for_content(animate=True)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def _sync_shimmer(self) -> None:
        want = self._content is not None and self._content.indeterminate
        running = (
            self._shimmer_anim.state()
            == QPropertyAnimation.State.Running
        )
        if want and not running:
            self._shimmer_anim.start()
        elif not want and running:
            self._stop_shimmer()

    def _stop_shimmer(self) -> None:
        self._shimmer_anim.stop()
        self._shimmer = 0.0

    def _reassert(self) -> None:
        hwnd = int(self.winId()) if self.isVisible() else 0
        if hwnd:
            window_style.reassert_topmost(hwnd)

    def _refresh_interactivity(self) -> None:
        """Make the island click-through when it has nothing to click.

        A collapsed, action-less pill sits exactly where a user reaches for
        a browser tab, so it must not swallow that click.
        """
        hwnd = int(self.winId())
        if not hwnd:
            return
        content = self._content
        interactive = bool(
            self._expanded
            or self._drag_active
            or (content is not None and content.actions)
        )
        window_style.set_click_through(hwnd, enabled=not interactive)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        hwnd = int(self.winId())
        window_style.apply_overlay_styles(hwnd)
        window_style.reassert_topmost(hwnd)
        self._refresh_interactivity()

    # ---- interaction -------------------------------------------------

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._hovered = True
        self._close_timer.stop()
        if self._content is not None and not self._expanded:
            self._open_timer.start()
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self._open_timer.stop()
        self._armed_destructive = None
        if self._expanded:
            self._close_timer.start()
        self.update()

    def _close_if_unhovered(self) -> None:
        if self._hovered:
            return
        content = self._content
        # Content that demanded attention stays open until it is retracted.
        if content is not None and content.auto_expand:
            return
        self.set_expanded(False)

    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint()
        for hit in self._hits:
            if not hit.rect.contains(pos):
                continue
            action = hit.action
            if action.destructive and self._armed_destructive is not action:
                # Arm on first click, fire on second. The island sits under
                # the cursor's travel path; a one-click "End task" there is
                # a footgun.
                self._armed_destructive = action
                self.update()
                return
            self._armed_destructive = None
            try:
                action.callback()
            except Exception as exc:  # a plugin must not kill the UI
                print(f"[island] action {action.label!r} failed: {exc}")
            self.update()
            return
        self.surface_clicked.emit()
        if not self._expanded:
            self.set_expanded(True)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        pos = event.position().toPoint()
        over = any(h.rect.contains(pos) for h in self._hits)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if over
            else Qt.CursorShape.ArrowCursor
        )

    # ---- drag and drop ----------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drag_active = True
            self._resize_for_content(animate=True)
            self.update()

    def dragLeaveEvent(self, event) -> None:
        super().dragLeaveEvent(event)
        self._drag_active = False
        self._resize_for_content(animate=True)
        self.update()

    def dropEvent(self, event) -> None:
        paths = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.isLocalFile()
        ]
        self._drag_active = False
        self._resize_for_content(animate=True)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        self.update()

    # ---- painting ----------------------------------------------------

    def _ensure_fonts(self) -> None:
        if self._fonts_built:
            return
        t = self._theme
        base = "Segoe UI Variable Text"
        self._f_title = QFont(base, t.metric("title_size"))
        self._f_title.setWeight(QFont.Weight.DemiBold)
        self._f_detail = QFont(base, t.metric("detail_size"))
        self._f_detail.setWeight(QFont.Weight.Normal)
        for f in (self._f_title, self._f_detail):
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self._fonts_built = True

    def _accent(self) -> QColor:
        content = self._content
        if content is None:
            return self._theme.color("accent_neutral")
        if content.accent:
            exact = QColor(content.accent)
            if exact.isValid():
                return exact
        return self._theme.accent_for(Urgency(content.urgency).name)

    def paintEvent(self, event) -> None:
        self._ensure_fonts()
        t = self._theme
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )

        pill = QRectF(self._pill_rect)
        radius = float(
            t.metric("radius_expanded")
            if self._expanded
            else t.metric("radius_collapsed")
        )

        self._paint_shadow(painter, pill, radius)

        surface = t.color(
            "surface_hover" if self._hovered or self._drag_active else "surface"
        )
        path = QPainterPath()
        path.addRoundedRect(pill, radius, radius)
        painter.fillPath(path, surface)

        border_w = t.metric("border_width")
        if border_w > 0:
            pen = QPen(t.color("border"))
            pen.setWidthF(float(border_w))
            painter.setPen(pen)
            painter.drawPath(path)

        if (
            t.effect("urgency_edge")
            and self._content is not None
            and self._content.urgency != Urgency.NEUTRAL
        ):
            self._paint_urgency_edge(painter, pill, radius)

        self._hits = []
        if self._content is None:
            painter.end()
            return

        painter.setOpacity(self._content_opacity)
        if self._drag_active:
            self._paint_drop_prompt(painter, pill)
        elif self._expanded:
            self._paint_expanded(painter, pill)
        else:
            self._paint_collapsed(painter, pill)
        painter.end()

    def _paint_shadow(
        self, painter: QPainter, pill: QRectF, radius: float
    ) -> None:
        blur = int(self._theme.effect("shadow_blur"))
        if blur <= 0:
            return
        base = self._theme.color("shadow")
        offset_y = float(self._theme.effect("shadow_offset_y"))
        steps = 7
        for i in range(steps, 0, -1):
            grow = blur * (i / steps)
            alpha = int(base.alpha() * (0.10 * (1.0 - i / (steps + 1))))
            if alpha <= 0:
                continue
            c = QColor(base)
            c.setAlpha(alpha)
            rect = pill.adjusted(-grow, -grow + offset_y, grow, grow + offset_y)
            p = QPainterPath()
            p.addRoundedRect(rect, radius + grow, radius + grow)
            painter.fillPath(p, c)

    def _paint_urgency_edge(
        self, painter: QPainter, pill: QRectF, radius: float
    ) -> None:
        """A hairline of accent colour along the top edge of the pill."""
        clip = QPainterPath()
        clip.addRoundedRect(pill, radius, radius)
        painter.save()
        painter.setClipPath(clip)
        edge = QRectF(pill.left(), pill.top(), pill.width(), 2.0)
        painter.fillRect(edge, self._accent())
        painter.restore()

    def _paint_collapsed(self, painter: QPainter, pill: QRectF) -> None:
        t = self._theme
        content = self._content
        assert content is not None
        pad = t.metric("padding_h")
        x = pill.left() + pad
        inner_w = pill.width() - 2 * pad

        if content.glyph:
            size = float(t.metric("glyph_size"))
            box = QRectF(x, pill.top() + (pill.height() - size) / 2.0, size, size)
            if glyphs.draw(painter, content.glyph, box, self._accent()):
                x += size + 7
                inner_w -= size + 7

        painter.setFont(self._f_title)
        painter.setPen(t.color("text"))
        fm = QFontMetrics(self._f_title)
        text = fm.elidedText(
            content.collapsed_text,
            Qt.TextElideMode.ElideRight,
            max(int(inner_w), 1),
        )
        painter.drawText(
            QRectF(x, pill.top(), max(inner_w, 1.0), pill.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )

        if content.progress is not None:
            bar = QRectF(
                pill.left() + pad,
                pill.bottom() - 4.0,
                pill.width() - 2 * pad,
                float(t.metric("progress_height")),
            )
            self._paint_progress(painter, bar, content.progress)

    def _paint_expanded(self, painter: QPainter, pill: QRectF) -> None:
        t = self._theme
        content = self._content
        assert content is not None
        pad_h = t.metric("padding_h")
        pad_v = t.metric("padding_v")
        x = pill.left() + pad_h
        y = pill.top() + pad_v
        width = pill.width() - 2 * pad_h

        if content.glyph:
            size = float(t.metric("glyph_size")) + 3.0
            box = QRectF(x, y + (20.0 - size) / 2.0, size, size)
            if glyphs.draw(painter, content.glyph, box, self._accent()):
                x += size + 9
                width -= size + 9

        painter.setFont(self._f_title)
        painter.setPen(t.color("text"))
        fm_t = QFontMetrics(self._f_title)
        painter.drawText(
            QRectF(x, y, width, 20.0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            fm_t.elidedText(
                content.title, Qt.TextElideMode.ElideRight, max(int(width), 1)
            ),
        )
        y += 20.0

        if content.detail:
            painter.setFont(self._f_detail)
            painter.setPen(t.color("text_dim"))
            fm_d = QFontMetrics(self._f_detail)
            painter.drawText(
                QRectF(x, y, width, 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                fm_d.elidedText(
                    content.detail,
                    Qt.TextElideMode.ElideRight,
                    max(int(width), 1),
                ),
            )
            y += 18.0

        if content.progress is not None:
            bar = QRectF(
                pill.left() + pad_h,
                y + 6.0,
                pill.width() - 2 * pad_h,
                float(t.metric("progress_height")),
            )
            self._paint_progress(painter, bar, content.progress)
            y = bar.bottom() + 6.0

        if content.actions:
            self._paint_actions(painter, pill, content.actions)

    def _paint_actions(
        self, painter: QPainter, pill: QRectF, actions: tuple[Action, ...]
    ) -> None:
        t = self._theme
        painter.setFont(self._f_detail)
        fm = QFontMetrics(self._f_detail)
        pad_h = t.metric("padding_h")
        height = 22.0
        y = pill.bottom() - height - t.metric("padding_v")
        x = pill.right() - pad_h

        for action in reversed(actions):
            armed = self._armed_destructive is action
            label = "Confirm?" if armed else action.label
            text_w = fm.horizontalAdvance(label)
            box_w = text_w + 20.0
            rect = QRectF(x - box_w, y, box_w, height)
            x = rect.left() - 8.0

            fill = QColor(
                t.color("accent_critical") if armed else t.color("border")
            )
            if armed:
                fill.setAlpha(210)
            path = QPainterPath()
            path.addRoundedRect(rect, height / 2.0, height / 2.0)
            painter.fillPath(path, fill)
            painter.setPen(t.color("text"))
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, label
            )
            self._hits.append(_Hit(rect.toRect(), action))

    def _paint_progress(
        self, painter: QPainter, bar: QRectF, progress: float
    ) -> None:
        t = self._theme
        radius = bar.height() / 2.0
        track = QPainterPath()
        track.addRoundedRect(bar, radius, radius)
        painter.fillPath(track, t.color("progress_track"))

        accent = self._accent()
        if progress < 0:
            # Indeterminate: a short segment sweeping left to right.
            seg_w = bar.width() * 0.28
            travel = bar.width() + seg_w
            left = bar.left() - seg_w + travel * self._shimmer
            seg = QRectF(
                max(left, bar.left()),
                bar.top(),
                min(seg_w, bar.right() - max(left, bar.left())),
                bar.height(),
            )
            if seg.width() > 0:
                p = QPainterPath()
                p.addRoundedRect(seg, radius, radius)
                painter.fillPath(p, accent)
            return

        frac = max(0.0, min(1.0, progress))
        if frac <= 0.0:
            return
        fill = QRectF(bar.left(), bar.top(), bar.width() * frac, bar.height())
        p = QPainterPath()
        p.addRoundedRect(fill, radius, radius)
        painter.fillPath(p, accent)

    def _paint_drop_prompt(self, painter: QPainter, pill: QRectF) -> None:
        t = self._theme
        painter.setFont(self._f_title)
        painter.setPen(t.color("text"))
        painter.drawText(
            pill,
            Qt.AlignmentFlag.AlignCenter,
            "Drop to extract text, compress or convert",
        )
        pen = QPen(self._accent())
        pen.setWidthF(1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        inner = pill.adjusted(6.0, 6.0, -6.0, -6.0)
        painter.drawRoundedRect(inner, 12.0, 12.0)

    # ---- misc --------------------------------------------------------

    def center_anchor(self) -> QPoint:
        if self._placement is None:
            return QPoint(0, 0)
        return self._placement.anchor
