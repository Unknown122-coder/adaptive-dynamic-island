"""Render the island's states to PNG files without needing a live desktop.

Run::

    .venv/Scripts/python.exe scripts/preview.py

Writes one image per state into ``build/preview/``. This exists for three
reasons: it catches painting regressions without a human watching a screen,
it produces the figures for the report, and it makes the island reviewable
in a pull request.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from core.island_window import IslandWindow
from core.model import Action, IslandContent, Priority, Urgency
from core.theme import load_theme

OUT = ROOT / "build" / "preview"

# A mid-grey backdrop proves the translucency and the shadow are working;
# on pure black or pure white both are invisible.
BACKDROP = QColor(78, 84, 92)


def states() -> list[tuple[str, IslandContent, bool]]:
    """(filename, content, expanded) triples covering the visual range."""
    return [
        (
            "01-ambient-collapsed",
            IslandContent(
                key="ambient.clock",
                priority=Priority.AMBIENT,
                title="9:41",
                collapsed="9:41  ·  Thu 27 Nov",
                glyph="clock",
            ),
            False,
        ),
        (
            "02-context-dev-collapsed",
            IslandContent(
                key="context.dev",
                priority=Priority.CONTEXT,
                title="main · 3 changed",
                detail="adaptive-dynamic-island",
                glyph="branch",
                urgency=Urgency.INFO,
            ),
            False,
        ),
        (
            "03-activity-progress-expanded",
            IslandContent(
                key="activity.pytest",
                priority=Priority.ACTIVITY,
                title="pytest · tests/",
                detail="312 of 740 passed · 00:18 elapsed",
                glyph="flask",
                progress=0.42,
                urgency=Urgency.INFO,
                collapsed="pytest 42%",
            ),
            True,
        ),
        (
            "04-activity-indeterminate",
            IslandContent(
                key="activity.docker",
                priority=Priority.ACTIVITY,
                title="docker build · api",
                detail="step 7 of 14 · exporting layers",
                glyph="cube",
                progress=-1.0,
                urgency=Urgency.NEUTRAL,
            ),
            True,
        ),
        (
            "05-alert-cpu",
            IslandContent(
                key="alert.cpu",
                priority=Priority.ALERT,
                title="node.exe is using 94% CPU",
                detail="sustained for 14s · system at 97%",
                glyph="cpu",
                urgency=Urgency.WARNING,
                actions=(
                    Action("Mute 10 min", lambda: None),
                    Action("End task", lambda: None, destructive=True),
                ),
                auto_expand=True,
            ),
            True,
        ),
        (
            "05b-alert-network",
            IslandContent(
                key="alert.network",
                priority=Priority.ALERT,
                title="Network disconnected",
                detail="no interface has a working address",
                glyph="wifi-off",
                progress=-1.0,
                urgency=Urgency.WARNING,
                auto_expand=True,
                collapsed="offline",
            ),
            True,
        ),
        (
            "06-privacy-watching",
            IslandContent(
                key="privacy.indicator",
                priority=Priority.AMBIENT,
                title="Privacy guard watching",
                detail="1 person in view",
                glyph="eye",
                collapsed="watching",
            ),
            False,
        ),
        (
            "06b-privacy-critical",
            IslandContent(
                key="privacy.shoulder",
                priority=Priority.PRIVACY,
                title="1 other person in view",
                detail="someone may be reading over your shoulder",
                glyph="faces",
                urgency=Urgency.CRITICAL,
                actions=(
                    Action("Dismiss", lambda: None),
                    Action("Lock now", lambda: None),
                ),
                auto_expand=True,
            ),
            True,
        ),
        (
            "07-drop-offer",
            IslandContent(
                key="drop.offer",
                priority=Priority.ACTIVITY,
                title="screenshot.png",
                detail="1.4 MB",
                glyph="download",
                urgency=Urgency.INFO,
                actions=(
                    Action("Extract text", lambda: None),
                    Action("Compress", lambda: None),
                    Action("To WebP", lambda: None),
                    Action("Cancel", lambda: None),
                ),
                auto_expand=True,
            ),
            True,
        ),
        (
            "08-drop-result",
            IslandContent(
                key="drop.result",
                priority=Priority.ACTIVITY,
                title="photo.min.jpg",
                detail="442.9 KB to 39.1 KB, 91% smaller",
                glyph="cube",
                urgency=Urgency.SUCCESS,
                actions=(Action("Show in folder", lambda: None),),
                auto_expand=True,
            ),
            True,
        ),
        (
            "09-clipboard-colour",
            IslandContent(
                key="clipboard.copied",
                priority=Priority.CONTEXT,
                title="#5B8CFF",
                detail="blue",
                glyph="sparkle",
                accent="#5B8CFF",
                collapsed="#5B8CFF",
            ),
            False,
        ),
        (
            "10-clipboard-secret",
            IslandContent(
                key="clipboard.secret",
                priority=Priority.ALERT,
                title="AWS access key copied",
                detail="hidden from the island and from the stack",
                glyph="key",
                urgency=Urgency.CRITICAL,
                actions=(
                    Action("Keep", lambda: None),
                    Action("Wipe now", lambda: None),
                ),
                auto_expand=True,
            ),
            True,
        ),
        (
            "11-plugin-pomodoro",
            IslandContent(
                key="plugin.pomodoro.main",
                priority=Priority.PLUGIN,
                title="Focus · 18:42",
                detail="2 completed",
                glyph="clock",
                progress=0.25,
                urgency=Urgency.INFO,
                actions=(Action("Stop", lambda: None),),
            ),
            True,
        ),
        (
            "12-meeting-success",
            IslandContent(
                key="context.meeting",
                priority=Priority.CONTEXT,
                title="Microphone live · 12:04",
                detail="Microsoft.Teams",
                glyph="mic",
                urgency=Urgency.SUCCESS,
                actions=(Action("Mute", lambda: None),),
            ),
            True,
        ),
    ]


def render(window: IslandWindow, name: str) -> Path:
    pixmap = window.grab()
    # ``grab`` returns device pixels tagged with the screen's scale factor.
    # Dropping the tag before compositing keeps the island at full
    # resolution; leaving it set makes Qt scale twice and the island ends
    # up occupying two thirds of the canvas with dead space around it.
    shot = pixmap.toImage()
    shot.setDevicePixelRatio(1.0)
    image = QImage(shot.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.0)
    image.fill(BACKDROP)
    painter = QPainter(image)
    painter.drawImage(0, 0, shot)
    painter.end()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    image.save(str(path))
    return path


def ink_coverage(path: Path) -> float:
    """Fraction of pixels that differ from the backdrop.

    A crude but effective regression guard: if a refactor breaks painting,
    the island renders as an empty rounded rectangle and this number
    collapses, which the preview run then reports instead of silently
    writing seven identical images.
    """
    image = QImage(str(path))
    if image.isNull():
        return 0.0
    bg = BACKDROP.rgb()
    differing = 0
    sampled = 0
    # Sampling every second pixel is plenty and keeps the run fast.
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            sampled += 1
            if image.pixel(x, y) != bg:
                differing += 1
    return differing / sampled if sampled else 0.0


def contact_sheet(paths: list[Path]) -> Path:
    """Stack every state into one image, which is easier to eyeball."""
    images = [QImage(str(p)) for p in paths]
    images = [im for im in images if not im.isNull()]
    if not images:
        raise RuntimeError("no state images rendered")
    gap = 14
    width = max(im.width() for im in images) + 2 * gap
    height = sum(im.height() + gap for im in images) + gap
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(32, 34, 38))
    painter = QPainter(sheet)
    y = gap
    for im in images:
        x = (width - im.width()) // 2
        painter.drawImage(x, y, im)
        painter.setPen(QColor(120, 126, 134))
        painter.drawRect(QRectF(x - 0.5, y - 0.5, im.width(), im.height()))
        y += im.height() + gap
    painter.end()
    path = OUT / "00-contact-sheet.png"
    sheet.save(str(path))
    return path


def main() -> int:
    app = QApplication(sys.argv)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, True)

    theme = load_theme(ROOT / "config" / "themes" / "midnight_oled.json")
    window = IslandWindow(theme)
    window.reposition(animate=False)

    written: list[Path] = []
    failures: list[str] = []
    for name, content, expanded in states():
        window.set_expanded(expanded)
        window.set_content(content, animate=False)
        if content.indeterminate:
            window._shimmer = 0.45
        app.processEvents()
        path = render(window, name)
        written.append(path)
        ink = ink_coverage(path)
        flag = "" if ink > 0.004 else "   <-- looks blank"
        if flag:
            failures.append(name)
        print(
            f"wrote {path.relative_to(ROOT)}  "
            f"({path.stat().st_size} bytes, ink {ink:.3%}){flag}"
        )

    sheet = contact_sheet(written)
    print(f"wrote {sheet.relative_to(ROOT)}")
    if failures:
        print(f"BLANK STATES: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
