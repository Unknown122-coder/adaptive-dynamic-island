r"""Where the island sits, and on which screen.

Two decisions live here, and both are places the usual clone gets it wrong.

**Which screen.** The island only makes sense on the panel that physically
has a webcam above it. On a docked laptop the *primary* screen is normally
the external monitor, so "primary" is the wrong default. We ask Windows for
the built-in panel (see :mod:`core.osbridge.display`) and match it to a Qt
screen by GDI device name, falling back to primary when there is no
internal panel at all, as on a desktop PC.

**Where on that screen.** Horizontally centred by default, flush to the top
edge, with a configurable nudge for machines whose camera is off-centre.
Positions are computed in Qt's logical pixels, so the result is already
correct on a 150% or 200% display without any manual DPI arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication, QScreen

from core.osbridge.display import internal_display_device_names


@dataclass(frozen=True)
class Placement:
    """Resolved anchor information, kept so the UI can explain itself."""

    screen_name: str
    is_internal: bool
    anchor: QPoint          # logical top-centre point the island hangs from
    screen_rect: QRect


def _screen_by_name(name: str) -> QScreen | None:
    for screen in QGuiApplication.screens():
        if screen.name() == name:
            return screen
    return None


def target_screen(preference: str = "internal") -> tuple[QScreen, bool]:
    """Pick the screen to anchor to.

    ``preference`` is ``"internal"``, ``"primary"``, or an exact Qt screen
    name. Returns the screen and whether it is the built-in panel.
    """
    screens = QGuiApplication.screens()
    primary = QGuiApplication.primaryScreen() or (screens[0] if screens else None)
    if primary is None:  # no display at all; caller must handle
        raise RuntimeError("no screens available")

    internal_names = internal_display_device_names()

    if preference not in ("internal", "primary"):
        explicit = _screen_by_name(preference)
        if explicit is not None:
            return explicit, explicit.name() in internal_names

    if preference == "internal":
        for name in internal_names:
            found = _screen_by_name(name)
            if found is not None:
                return found, True

    return primary, primary.name() in internal_names


def resolve(
    width: int,
    height: int,
    top_inset: int = 1,
    horizontal_offset: int = 0,
    preference: str = "internal",
) -> tuple[QRect, Placement]:
    """Compute the island's logical rect plus a description of the anchor.

    ``horizontal_offset`` shifts the island right (positive) or left
    (negative) from centre, for laptops whose camera is not centred.
    """
    screen, is_internal = target_screen(preference)
    area = screen.geometry()  # full screen, not availableGeometry: we sit
    # above the work area on purpose, over the top edge.
    center_x = area.left() + area.width() // 2 + horizontal_offset
    x = center_x - width // 2
    y = area.top() + top_inset

    # Never let a wide expanded state or a big offset push the island off
    # the panel: clamp into the screen with a small margin.
    margin = 4
    x = max(area.left() + margin, min(x, area.right() - width - margin + 1))

    rect = QRect(x, y, width, height)
    placement = Placement(
        screen_name=screen.name(),
        is_internal=is_internal,
        anchor=QPoint(center_x, area.top() + top_inset),
        screen_rect=area,
    )
    return rect, placement


def rect_for_size(placement: Placement, width: int, height: int) -> QRect:
    """Re-centre a new size on an existing anchor.

    Used for the morph animation: collapsed and expanded states must share
    a centre line, otherwise the island appears to slide sideways as it
    grows, which reads as a bug rather than as motion design.
    """
    area = placement.screen_rect
    x = placement.anchor.x() - width // 2
    margin = 4
    x = max(area.left() + margin, min(x, area.right() - width - margin + 1))
    return QRect(x, placement.anchor.y(), width, height)


def describe(placement: Placement) -> str:
    kind = "built-in panel" if placement.is_internal else "external/primary"
    r = placement.screen_rect
    return (
        f"{placement.screen_name} ({kind}) "
        f"{r.width()}x{r.height()} at {r.left()},{r.top()}"
    )
