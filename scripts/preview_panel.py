"""Render the control panel to a PNG, the way preview.py does the island.

Same reasoning: a UI change should be reviewable without someone sitting
in front of a screen, and a layout regression should fail a run rather
than wait to be noticed.

    .venv/Scripts/python.exe scripts/preview_panel.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from core.control_panel import ControlPanel
from core.theme import load_theme

OUT = ROOT / "build" / "preview"
BACKDROP = QColor(78, 84, 92)

ENABLED = {"clock", "context", "activities", "drop", "plugins"}


class FakePlugin:
    def __init__(self, pid, name, active, healthy=True):
        self.id = pid
        self.name = name
        self.active = active
        self.healthy = healthy
        self.disabled_reason = None if healthy else "disabled after 3 failures"
        self.description = ""


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)

    theme = load_theme(ROOT / "config" / "themes" / "midnight_oled.json")
    panel = ControlPanel(theme, lambda key: key in ENABLED)
    panel.set_plugins(
        [
            FakePlugin("pomodoro", "Pomodoro timer", True),
            FakePlugin("battery", "Battery warnings", False),
        ]
    )
    panel.set_themes(["midnight_oled"], "midnight_oled")
    panel.adjustSize()
    app.processEvents()

    pixmap = panel.grab()
    shot = pixmap.toImage()
    shot.setDevicePixelRatio(1.0)
    image = QImage(shot.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.0)
    image.fill(BACKDROP)
    painter = QPainter(image)
    painter.drawImage(0, 0, shot)
    painter.end()

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "20-control-panel.png"
    image.save(str(path))
    print(f"wrote {path.relative_to(ROOT)}  ({panel.width()}x{panel.height()})")

    if panel.height() < 150 or panel.width() < 300:
        print("FAILED: the panel collapsed to nothing")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
