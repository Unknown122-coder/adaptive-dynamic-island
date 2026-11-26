"""Priority arbitration between content providers.

Providers never talk to the window. They publish into the arbiter, the
arbiter decides what deserves the screen, and exactly one ``content_changed``
signal reaches the UI thread. Consequences worth stating, because they are
the design's whole point:

* A provider can be added or removed at runtime without touching the window.
* Two providers shouting at once is resolved by policy, not by whichever
  timer happened to fire last, and ties fall back to publication order
  rather than to a clock too coarse to separate them.
* The window has a single input, so it has a single animation path, which is
  what keeps the motion coherent instead of janky.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from core.model import IslandContent, Priority


class ContentArbiter(QObject):
    """Holds live content from every provider and publishes the winner."""

    # Emitted with the winning content, or None when the island should idle.
    content_changed = Signal(object)
    # Emitted whenever the set of candidates changes, for the tray debug view.
    candidates_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: dict[str, IslandContent] = {}
        self._lock = threading.RLock()
        self._current_key: str | None = None

        # Expiry sweep. One timer for every provider beats one timer each.
        self._sweeper = QTimer(self)
        self._sweeper.setInterval(500)
        self._sweeper.timeout.connect(self._sweep)
        self._sweeper.start()

    # ---- provider API ------------------------------------------------

    def publish(self, content: IslandContent) -> None:
        """Add or update content. Safe to call from any thread."""
        with self._lock:
            content.touch()
            self._items[content.key] = content
        self._reevaluate()

    def retract(self, key: str) -> None:
        """Remove content by key. No-op when absent."""
        with self._lock:
            existed = self._items.pop(key, None) is not None
        if existed:
            self._reevaluate()

    def retract_prefix(self, prefix: str) -> None:
        """Remove every item whose key starts with ``prefix``.

        Used when a whole provider goes quiet, e.g. the context engine
        clearing all ``context.*`` content on a profile switch.
        """
        with self._lock:
            doomed = [k for k in self._items if k.startswith(prefix)]
            for k in doomed:
                del self._items[k]
        if doomed:
            self._reevaluate()

    # ---- introspection ----------------------------------------------

    def candidates(self) -> list[IslandContent]:
        with self._lock:
            return sorted(
                self._items.values(),
                key=lambda c: (int(c.priority), c.seq),
                reverse=True,
            )

    def current(self) -> IslandContent | None:
        with self._lock:
            if self._current_key is None:
                return None
            return self._items.get(self._current_key)

    # ---- internals ---------------------------------------------------

    def _sweep(self) -> None:
        with self._lock:
            doomed = [k for k, c in self._items.items() if c.expired]
            for k in doomed:
                del self._items[k]
        if doomed:
            self._reevaluate()

    def _winner(self) -> IslandContent | None:
        with self._lock:
            if not self._items:
                return None
            # Highest priority first; among equals, the freshest update.
            return max(
                self._items.values(),
                key=lambda c: (int(c.priority), c.seq),
            )

    def _reevaluate(self) -> None:
        win = self._winner()
        key = win.key if win else None
        changed_item = key != self._current_key
        self._current_key = key
        self.candidates_changed.emit()
        # Always re-emit: even without a key change the payload may have moved
        # (progress ticked up), and the window needs that to repaint.
        self.content_changed.emit(win)
        if changed_item:
            # Deliberately separate so future logging/metrics can distinguish
            # a swap from an in-place update without re-deriving it.
            pass


class ProviderHandle:
    """Convenience wrapper so a provider owns a namespace of keys.

    Keeps providers from colliding on key names and gives each one a single
    ``clear()`` that cannot accidentally retract someone else's content.
    """

    def __init__(self, arbiter: ContentArbiter, namespace: str) -> None:
        self._arbiter = arbiter
        self._ns = namespace.rstrip(".") + "."

    def key(self, suffix: str) -> str:
        return self._ns + suffix

    def publish(
        self,
        suffix: str,
        priority: Priority,
        title: str,
        **kwargs: object,
    ) -> IslandContent:
        content = IslandContent(
            key=self.key(suffix),
            priority=priority,
            title=title,
            **kwargs,  # type: ignore[arg-type]
        )
        self._arbiter.publish(content)
        return content

    def publish_content(self, content: IslandContent) -> None:
        if not content.key.startswith(self._ns):
            content.key = self.key(content.key)
        self._arbiter.publish(content)

    def retract(self, suffix: str) -> None:
        self._arbiter.retract(self.key(suffix))

    def clear(self) -> None:
        self._arbiter.retract_prefix(self._ns)

    @staticmethod
    def ttl(seconds: float) -> float:
        return time.time() + seconds
