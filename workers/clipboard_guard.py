"""The clipboard stack, and the secret guard that is the point of it.

Windows already has a clipboard history, so a plain re-implementation
would be pointless. Two things it does not do, and this does:

**It treats credentials as hazardous.** Win+V remembers everything,
including the token you pasted into a terminal thirty seconds ago, and
with clipboard sync enabled that history leaves the machine. Here, a copy
that looks like a credential is never displayed, is excluded from the
stack, and can be wiped from the clipboard with one click or
automatically after a timeout.

**It tells you what you copied.** A hex colour shows its swatch, a unix
timestamp shows the date, malformed JSON says so immediately rather than
at the moment you paste it.

The stack lives in memory only. Nothing is written to disk, which is both
a deliberate privacy property and the reason this cannot be a general
clipboard manager: close the island and the stack is gone.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QClipboard, QGuiApplication

from core import secrets, textkind
from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.model import Action, IslandContent, Priority, Urgency

# A copy is a transient event. The island shows what happened, then gets
# out of the way, rather than parking a clipboard widget on screen.
NOTICE_TTL = 5.0
SECRET_TTL = 25.0
MAX_ENTRY_CHARS = 256 * 1024


@dataclass
class Entry:
    """One remembered clipboard item. Never contains a detected secret."""

    text: str
    insight: textkind.Insight
    at: float = field(default_factory=time.time)

    @property
    def age_label(self) -> str:
        seconds = max(0, int(time.time() - self.at))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        return f"{seconds // 3600}h ago"


class ClipboardGuard(QObject):
    """Watches the clipboard, classifies copies and guards credentials."""

    secret_detected = Signal(str)   # human label, for a tray notification
    clipboard_wiped = Signal()

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "clipboard")
        self._settings = settings
        self._entries: deque[Entry] = deque(
            maxlen=settings.integer("clipboard.stack_size", 5)
        )
        self._running = False
        self._suppress_next = False
        self._last_text = ""
        self._pending_secret: str | None = None

        self._auto_wipe = QTimer(self)
        self._auto_wipe.setSingleShot(True)
        self._auto_wipe.timeout.connect(self._auto_wipe_fired)

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.dataChanged.connect(self._on_clipboard_changed)
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            # Qt raises if the connection is already gone, which happens
            # when the application is shutting down around us.
            with contextlib.suppress(RuntimeError, TypeError):
                clipboard.dataChanged.disconnect(self._on_clipboard_changed)
        self._running = False
        self._auto_wipe.stop()
        # The stack is memory-only, and stopping the feature should leave
        # nothing behind.
        self._entries.clear()
        self._last_text = ""
        self._handle.clear()

    @property
    def running(self) -> bool:
        return self._running

    def entries(self) -> list[Entry]:
        return list(self._entries)

    # ---- clipboard ---------------------------------------------------

    def _on_clipboard_changed(self) -> None:
        if not self._running:
            return
        if self._suppress_next:
            # Our own write, from a wipe or a restore.
            self._suppress_next = False
            return

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        text = clipboard.text(QClipboard.Mode.Clipboard)
        if not text or text == self._last_text:
            return
        if len(text) > MAX_ENTRY_CHARS:
            # Very large copies are files or documents; scanning and
            # storing them is not worth the memory.
            self._last_text = text
            return

        self._last_text = text
        found = secrets.scan(text)
        if found:
            self._handle_secret(text, found)
            return
        self._handle_normal(text)

    # ---- normal copies -----------------------------------------------

    def _handle_normal(self, text: str) -> None:
        self._auto_wipe.stop()
        self._pending_secret = None
        insight = textkind.describe(text)
        entry = Entry(text=text, insight=insight)
        self._entries.appendleft(entry)

        actions: tuple[Action, ...] = ()
        if len(self._entries) > 1:
            previous = self._entries[1]
            actions = (
                Action(
                    f"Re-copy {_short(previous.insight.title)}",
                    lambda t=previous.text: self._restore(t),
                ),
            )

        self._handle.publish_content(
            IslandContent(
                key="clipboard.copied",
                priority=Priority.CONTEXT,
                title=insight.title,
                detail=insight.detail or "copied",
                glyph=insight.glyph,
                urgency=Urgency.NEUTRAL,
                accent=insight.accent,
                actions=actions,
                collapsed=_short(insight.title, 36),
                expires_at=ProviderHandle.ttl(NOTICE_TTL),
            )
        )

    def _restore(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        self._suppress_next = True
        self._last_text = text
        clipboard.setText(text)

    # ---- credentials -------------------------------------------------

    def _handle_secret(self, text: str, found: list[secrets.SecretMatch]) -> None:
        label = secrets.summarise(found)
        severity = secrets.highest_severity(found)
        self._pending_secret = text

        # The secret is deliberately not added to the stack. A clipboard
        # history that remembers credentials is the problem, not a feature.
        urgency = (
            Urgency.CRITICAL
            if severity == secrets.Severity.HIGH
            else Urgency.WARNING
        )

        seconds = self._settings.number("clipboard.auto_wipe_seconds", 0)
        actions = [Action("Wipe now", self.wipe, destructive=False)]
        if seconds > 0:
            self._auto_wipe.start(int(seconds * 1000))
            detail = f"hidden · clipboard clears in {int(seconds)}s"
        else:
            actions.append(Action("Keep", self._keep))
            detail = "hidden from the island and from the stack"

        self._handle.publish_content(
            IslandContent(
                key="clipboard.secret",
                priority=Priority.ALERT,
                title=f"{label} copied",
                detail=detail,
                glyph="key",
                urgency=urgency,
                actions=tuple(actions),
                auto_expand=True,
                collapsed=f"{label} hidden",
                expires_at=ProviderHandle.ttl(SECRET_TTL),
            )
        )
        self.secret_detected.emit(label)

    def wipe(self) -> None:
        """Clear the clipboard and forget the pending secret."""
        self._auto_wipe.stop()
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            self._suppress_next = True
            clipboard.clear(QClipboard.Mode.Clipboard)
        self._pending_secret = None
        self._last_text = ""
        self._handle.retract("secret")
        self._handle.publish_content(
            IslandContent(
                key="clipboard.copied",
                priority=Priority.CONTEXT,
                title="Clipboard cleared",
                detail="the credential is gone",
                glyph="shield",
                urgency=Urgency.SUCCESS,
                collapsed="clipboard cleared",
                expires_at=ProviderHandle.ttl(4),
            )
        )
        self.clipboard_wiped.emit()

    def _keep(self) -> None:
        """Dismiss the warning, leaving the clipboard alone."""
        self._auto_wipe.stop()
        self._pending_secret = None
        self._handle.retract("secret")

    def _auto_wipe_fired(self) -> None:
        if self._pending_secret is None:
            return
        clipboard = QGuiApplication.clipboard()
        # Only wipe if the credential is still the thing on the clipboard.
        # Clearing something the user copied afterwards would be its own
        # kind of data loss.
        if clipboard is not None:
            current = clipboard.text(QClipboard.Mode.Clipboard)
            if current != self._pending_secret:
                self._pending_secret = None
                return
        self.wipe()


def _short(text: str, limit: int = 24) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
