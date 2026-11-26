"""A small vector icon set drawn with QPainterPath.

Why not an icon font? The obvious choice is Segoe Fluent Icons, but it only
ships on Windows 11. Windows 10 has Segoe MDL2 Assets instead, with
different codepoints, so an icon font means either dropping Windows 10 or
maintaining two codepoint tables and hoping each glyph means the same thing
in both. Shipping a font of our own adds a licence to audit and megabytes to
the installer.

Fourteen icons drawn as paths cost a few hundred lines, render identically
on every Windows version, scale cleanly to any DPI, and take their colour
from the theme. Content refers to them by name (``glyph="branch"``), so
plugin authors never touch a codepoint.

Every glyph is drawn on a 24x24 design grid and scaled to the target rect.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

GRID = 24.0

GlyphFn = Callable[[QPainter, float], None]
_REGISTRY: dict[str, GlyphFn] = {}


def glyph(name: str) -> Callable[[GlyphFn], GlyphFn]:
    def wrap(fn: GlyphFn) -> GlyphFn:
        _REGISTRY[name] = fn
        return fn

    return wrap


def available() -> list[str]:
    return sorted(_REGISTRY)


def draw(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: QColor,
    stroke: float = 1.9,
) -> bool:
    """Draw glyph ``name`` centred in ``rect``. False when unknown."""
    fn = _REGISTRY.get(name)
    if fn is None:
        return False
    size = min(rect.width(), rect.height())
    if size <= 0:
        return False
    scale = size / GRID
    painter.save()
    painter.translate(
        rect.left() + (rect.width() - size) / 2.0,
        rect.top() + (rect.height() - size) / 2.0,
    )
    painter.scale(scale, scale)
    pen = QPen(color)
    # The pen is set inside the scaled coordinate system, so a constant
    # design-grid width gives a stroke that thickens with the icon rather
    # than staying hairline-thin at large sizes.
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    fn(painter, stroke)
    painter.restore()
    return True


def _dot(painter: QPainter, x: float, y: float, r: float) -> None:
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(x, y), r, r)
    painter.restore()


# ---- source control ------------------------------------------------------


@glyph("branch")
def _branch(painter: QPainter, _s: float) -> None:
    # Trunk with a branch curving off to a third node: the familiar
    # version-control fork.
    painter.drawLine(QPointF(7, 7), QPointF(7, 17))
    _dot(painter, 7, 5.5, 2.1)
    _dot(painter, 7, 18.5, 2.1)
    _dot(painter, 17, 5.5, 2.1)
    path = QPainterPath(QPointF(17, 7.6))
    path.cubicTo(QPointF(17, 12), QPointF(7, 10.5), QPointF(7, 14))
    painter.drawPath(path)


@glyph("commit")
def _commit(painter: QPainter, _s: float) -> None:
    painter.drawLine(QPointF(2.5, 12), QPointF(8.5, 12))
    painter.drawLine(QPointF(15.5, 12), QPointF(21.5, 12))
    painter.drawEllipse(QPointF(12, 12), 3.5, 3.5)


# ---- audio and video -----------------------------------------------------


@glyph("mic")
def _mic(painter: QPainter, _s: float) -> None:
    body = QPainterPath()
    body.addRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
    painter.drawPath(body)
    arc = QPainterPath()
    arc.arcMoveTo(QRectF(6, 7.5, 12, 12), 200)
    arc.arcTo(QRectF(6, 7.5, 12, 12), 200, 140)
    painter.drawPath(arc)
    painter.drawLine(QPointF(12, 19.5), QPointF(12, 21.5))


@glyph("mic-off")
def _mic_off(painter: QPainter, _s: float) -> None:
    _mic(painter, _s)
    painter.drawLine(QPointF(5, 4.5), QPointF(19, 20))


@glyph("camera")
def _camera(painter: QPainter, _s: float) -> None:
    body = QPainterPath()
    body.addRoundedRect(QRectF(2.5, 7, 14, 11), 2.5, 2.5)
    painter.drawPath(body)
    lens = QPainterPath()
    lens.moveTo(QPointF(16.5, 11.5))
    lens.lineTo(QPointF(21.5, 8.5))
    lens.lineTo(QPointF(21.5, 16.5))
    lens.lineTo(QPointF(16.5, 13.5))
    lens.closeSubpath()
    painter.drawPath(lens)


@glyph("eye")
def _eye(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(2.5, 12))
    path.cubicTo(QPointF(6, 5.5), QPointF(18, 5.5), QPointF(21.5, 12))
    path.cubicTo(QPointF(18, 18.5), QPointF(6, 18.5), QPointF(2.5, 12))
    painter.drawPath(path)
    painter.drawEllipse(QPointF(12, 12), 3.0, 3.0)


# ---- privacy -------------------------------------------------------------


@glyph("shield")
def _shield(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(12, 2.5))
    path.lineTo(QPointF(20, 6))
    path.lineTo(QPointF(20, 12))
    path.cubicTo(QPointF(20, 17.5), QPointF(16.5, 20.5), QPointF(12, 21.5))
    path.cubicTo(QPointF(7.5, 20.5), QPointF(4, 17.5), QPointF(4, 12))
    path.lineTo(QPointF(4, 6))
    path.closeSubpath()
    painter.drawPath(path)


@glyph("shield-alert")
def _shield_alert(painter: QPainter, _s: float) -> None:
    _shield(painter, _s)
    painter.drawLine(QPointF(12, 8.5), QPointF(12, 13.5))
    _dot(painter, 12, 16.5, 1.1)


@glyph("faces")
def _faces(painter: QPainter, _s: float) -> None:
    # Two overlapping head-and-shoulders: the shoulder-surfing warning.
    painter.drawEllipse(QPointF(9, 8.5), 3.4, 3.4)
    arc = QPainterPath()
    arc.moveTo(QPointF(3.5, 19.5))
    arc.cubicTo(QPointF(4, 14.5), QPointF(14, 14.5), QPointF(14.5, 19.5))
    painter.drawPath(arc)
    painter.drawEllipse(QPointF(17.5, 8.0), 2.6, 2.6)
    arc2 = QPainterPath()
    arc2.moveTo(QPointF(14.5, 17.5))
    arc2.cubicTo(QPointF(16, 13.5), QPointF(21.5, 14.0), QPointF(21.5, 18.5))
    painter.drawPath(arc2)


@glyph("lock")
def _lock(painter: QPainter, _s: float) -> None:
    body = QPainterPath()
    body.addRoundedRect(QRectF(4.5, 10.5, 15, 10), 2.5, 2.5)
    painter.drawPath(body)
    shackle = QPainterPath()
    shackle.arcMoveTo(QRectF(8, 3.5, 8, 10), 0)
    shackle.arcTo(QRectF(8, 3.5, 8, 10), 0, 180)
    painter.drawPath(shackle)


# ---- system --------------------------------------------------------------


@glyph("cpu")
def _cpu(painter: QPainter, _s: float) -> None:
    outer = QPainterPath()
    outer.addRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
    painter.drawPath(outer)
    inner = QPainterPath()
    inner.addRoundedRect(QRectF(9.5, 9.5, 5, 5), 1, 1)
    painter.drawPath(inner)
    for offset in (9.0, 12.0, 15.0):
        painter.drawLine(QPointF(offset, 2.5), QPointF(offset, 6))
        painter.drawLine(QPointF(offset, 18), QPointF(offset, 21.5))
        painter.drawLine(QPointF(2.5, offset), QPointF(6, offset))
        painter.drawLine(QPointF(18, offset), QPointF(21.5, offset))


@glyph("pulse")
def _pulse(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(2.5, 12))
    for point in (
        (7, 12),
        (9, 5.5),
        (12, 18.5),
        (15, 9),
        (17, 12),
        (21.5, 12),
    ):
        path.lineTo(QPointF(*point))
    painter.drawPath(path)


@glyph("warning")
def _warning(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(12, 3.5))
    path.lineTo(QPointF(21.5, 19.5))
    path.lineTo(QPointF(2.5, 19.5))
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(QPointF(12, 10), QPointF(12, 14.5))
    _dot(painter, 12, 17, 1.0)


@glyph("wifi-off")
def _wifi_off(painter: QPainter, _s: float) -> None:
    for rect in (QRectF(2, 5, 20, 20), QRectF(6, 9, 12, 12)):
        arc = QPainterPath()
        arc.arcMoveTo(rect, 35)
        arc.arcTo(rect, 35, 110)
        painter.drawPath(arc)
    _dot(painter, 12, 18.5, 1.3)
    painter.drawLine(QPointF(4.5, 4.0), QPointF(19.5, 20.0))


# ---- work ----------------------------------------------------------------


@glyph("flask")
def _flask(painter: QPainter, _s: float) -> None:
    painter.drawLine(QPointF(9, 3), QPointF(15, 3))
    path = QPainterPath(QPointF(10, 3))
    path.lineTo(QPointF(10, 9.5))
    path.lineTo(QPointF(4.5, 18.5))
    path.cubicTo(QPointF(3.5, 20.3), QPointF(5, 21.5), QPointF(7, 21.5))
    path.lineTo(QPointF(17, 21.5))
    path.cubicTo(QPointF(19, 21.5), QPointF(20.5, 20.3), QPointF(19.5, 18.5))
    path.lineTo(QPointF(14, 9.5))
    path.lineTo(QPointF(14, 3))
    painter.drawPath(path)
    painter.drawLine(QPointF(7.5, 15), QPointF(16.5, 15))


@glyph("cube")
def _cube(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(12, 2.8))
    path.lineTo(QPointF(20.5, 7.4))
    path.lineTo(QPointF(20.5, 16.6))
    path.lineTo(QPointF(12, 21.2))
    path.lineTo(QPointF(3.5, 16.6))
    path.lineTo(QPointF(3.5, 7.4))
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(QPointF(3.5, 7.4), QPointF(12, 12))
    painter.drawLine(QPointF(20.5, 7.4), QPointF(12, 12))
    painter.drawLine(QPointF(12, 12), QPointF(12, 21.2))


@glyph("download")
def _download(painter: QPainter, _s: float) -> None:
    painter.drawLine(QPointF(12, 3), QPointF(12, 14.5))
    arrow = QPainterPath(QPointF(7, 10))
    arrow.lineTo(QPointF(12, 15))
    arrow.lineTo(QPointF(17, 10))
    painter.drawPath(arrow)
    tray = QPainterPath(QPointF(3.5, 16))
    tray.lineTo(QPointF(3.5, 20.5))
    tray.lineTo(QPointF(20.5, 20.5))
    tray.lineTo(QPointF(20.5, 16))
    painter.drawPath(tray)


@glyph("clipboard")
def _clipboard(painter: QPainter, _s: float) -> None:
    body = QPainterPath()
    body.addRoundedRect(QRectF(4.5, 4.5, 15, 17), 2.5, 2.5)
    painter.drawPath(body)
    tab = QPainterPath()
    tab.addRoundedRect(QRectF(8.5, 2, 7, 5), 1.5, 1.5)
    painter.drawPath(tab)
    painter.drawLine(QPointF(8, 12), QPointF(16, 12))
    painter.drawLine(QPointF(8, 16), QPointF(13.5, 16))


@glyph("key")
def _key(painter: QPainter, _s: float) -> None:
    painter.drawEllipse(QPointF(7.5, 16.5), 4.0, 4.0)
    painter.drawLine(QPointF(10.4, 13.6), QPointF(20.5, 3.5))
    painter.drawLine(QPointF(17.5, 6.5), QPointF(19.5, 8.5))
    painter.drawLine(QPointF(15, 9), QPointF(17, 11))


@glyph("text")
def _text(painter: QPainter, _s: float) -> None:
    painter.drawLine(QPointF(4, 6), QPointF(20, 6))
    painter.drawLine(QPointF(12, 6), QPointF(12, 20))
    painter.drawLine(QPointF(8.5, 20), QPointF(15.5, 20))


@glyph("clock")
def _clock(painter: QPainter, _s: float) -> None:
    painter.drawEllipse(QPointF(12, 12), 9.0, 9.0)
    painter.drawLine(QPointF(12, 6.5), QPointF(12, 12))
    painter.drawLine(QPointF(12, 12), QPointF(16, 14.5))


@glyph("sparkle")
def _sparkle(painter: QPainter, _s: float) -> None:
    path = QPainterPath(QPointF(12, 3))
    path.cubicTo(QPointF(13, 9.5), QPointF(14.5, 11), QPointF(21, 12))
    path.cubicTo(QPointF(14.5, 13), QPointF(13, 14.5), QPointF(12, 21))
    path.cubicTo(QPointF(11, 14.5), QPointF(9.5, 13), QPointF(3, 12))
    path.cubicTo(QPointF(9.5, 11), QPointF(11, 9.5), QPointF(12, 3))
    painter.drawPath(path)
