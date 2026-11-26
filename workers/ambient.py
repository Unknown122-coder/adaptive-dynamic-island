"""The resting state: a clock, so the island is never an empty black blob.

Deliberately the lowest priority in the system. Anything with something to
say outranks it, and it reappears when they fall silent.

The timer realigns itself to the next minute boundary after every tick
instead of running at one second. Over an eight-hour session that is 480
wake-ups instead of 28,800, which is the difference between an overlay you
leave running and one you quit to save battery.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer

from core.arbiter import ContentArbiter, ProviderHandle
from core.model import IslandContent, Priority


class AmbientProvider(QObject):
    def __init__(
        self,
        arbiter: ContentArbiter,
        time_format: str = "%H:%M",
        date_format: str = "%a %d %b",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "ambient")
        self._time_format = time_format
        self._date_format = date_format
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        self._handle.clear()

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.localtime()
        clock = time.strftime(self._time_format, now)
        date = time.strftime(self._date_format, now)
        self._handle.publish_content(
            IslandContent(
                key="ambient.clock",
                priority=Priority.AMBIENT,
                title=clock,
                detail=date,
                glyph="clock",
                collapsed=f"{clock}  ·  {date}",
            )
        )
        # Re-arm for the top of the next minute, plus a small margin so a
        # slightly early wake-up does not show the previous minute.
        remaining = 60 - now.tm_sec
        self._timer.start(max(1, remaining) * 1000 + 250)
