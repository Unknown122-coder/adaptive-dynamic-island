"""The public API a plugin codes against.

A plugin is a single Python file dropped into the plugins folder. It
subclasses :class:`IslandPlugin`, and the island discovers it, gives it a
namespace to publish into, and manages its lifetime.

The design goal is that writing a plugin requires knowing almost nothing
about the island. A plugin never touches the window, never sees the
arbiter, and never manages a thread or a timer's lifetime. It gets a
context object with four verbs and that is the whole surface.

    from core.plugin_api import IslandPlugin, PluginContext

    class Hello(IslandPlugin):
        id = "hello"
        name = "Hello"

        def setup(self, ctx: PluginContext) -> None:
            ctx.every(1000, lambda: ctx.show("Hello", detail="from a plugin"))

Two guarantees the host makes, and both are enforced rather than promised:

**A plugin cannot outrank a warning.** Everything a plugin publishes is
clamped to ``Priority.PLUGIN``, which sits below live activities, system
alerts and privacy warnings. A plugin cannot bury something that protects
the user, however it is written.

**A plugin cannot take the island down.** Every call into plugin code is
wrapped. A plugin that raises is reported and, after repeated failures,
disabled for the session; the island carries on.

What is *not* guaranteed: isolation. A plugin is ordinary Python running
in the island's process with the user's privileges. It can read files and
open sockets. Installing one is exactly as much of a decision as running
any other script, and the documentation says so rather than implying a
sandbox that does not exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer

from core.arbiter import ProviderHandle
from core.model import Action, IslandContent, Priority, Urgency

# Plugins are capped here. See the module docstring.
MAX_PLUGIN_PRIORITY = Priority.PLUGIN


class PluginContext:
    """Everything a plugin is given. Created by the host, never by a plugin."""

    def __init__(
        self,
        plugin_id: str,
        handle: ProviderHandle,
        settings: dict[str, Any],
        owner: QObject,
        logger: Callable[[str], None],
    ) -> None:
        self._id = plugin_id
        self._handle = handle
        self._settings = settings
        self._owner = owner
        self._log = logger
        self._timers: list[QTimer] = []

    # ---- publishing ---------------------------------------------------

    def show(
        self,
        title: str,
        detail: str | None = None,
        glyph: str | None = None,
        progress: float | None = None,
        urgency: str = "neutral",
        actions: tuple[Action, ...] = (),
        collapsed: str | None = None,
        ttl: float | None = None,
        slot: str = "main",
        accent: str | None = None,
    ) -> None:
        """Put something on the island.

        Calling it again with the same ``slot`` updates in place, so a
        counting timer does not replay the entrance animation every second.
        """
        self._handle.publish_content(
            IslandContent(
                key=slot,
                priority=MAX_PLUGIN_PRIORITY,
                title=title,
                detail=detail,
                glyph=glyph,
                progress=progress,
                urgency=_URGENCY.get(str(urgency).lower(), Urgency.NEUTRAL),
                accent=accent,
                actions=tuple(actions),
                collapsed=collapsed,
                expires_at=(ProviderHandle.ttl(ttl) if ttl else None),
            )
        )

    def hide(self, slot: str = "main") -> None:
        self._handle.retract(slot)

    def hide_all(self) -> None:
        self._handle.clear()

    @staticmethod
    def action(
        label: str, callback: Callable[[], None], destructive: bool = False
    ) -> Action:
        """Build a button for :meth:`show`."""
        return Action(label=label, callback=callback, destructive=destructive)

    # ---- scheduling ---------------------------------------------------

    def every(self, milliseconds: int, callback: Callable[[], None]) -> QTimer:
        """A repeating timer the host stops when the plugin is unloaded.

        Plugins forgetting to stop timers is the usual way a plugin system
        leaks, so the host owns them.
        """
        timer = QTimer(self._owner)
        timer.setInterval(max(50, int(milliseconds)))
        timer.timeout.connect(self._guarded(callback))
        timer.start()
        self._timers.append(timer)
        return timer

    def after(self, milliseconds: int, callback: Callable[[], None]) -> QTimer:
        """A one-shot timer, also owned by the host."""
        timer = QTimer(self._owner)
        timer.setSingleShot(True)
        timer.setInterval(max(0, int(milliseconds)))
        timer.timeout.connect(self._guarded(callback))
        timer.start()
        self._timers.append(timer)
        return timer

    def _guarded(self, callback: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                callback()
            except Exception as exc:
                self._log(f"callback failed: {exc}")

        return run

    def _stop_timers(self) -> None:
        for timer in self._timers:
            timer.stop()
            timer.deleteLater()
        self._timers.clear()

    # ---- settings and logging ------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        """Read a value from this plugin's own settings block."""
        return self._settings.get(key, default)

    def log(self, message: str) -> None:
        self._log(message)

    @property
    def id(self) -> str:
        return self._id


_URGENCY = {
    "neutral": Urgency.NEUTRAL,
    "info": Urgency.INFO,
    "success": Urgency.SUCCESS,
    "warning": Urgency.WARNING,
    "critical": Urgency.CRITICAL,
}


class IslandPlugin(ABC):
    """Base class for every plugin.

    Subclass it in a ``.py`` file inside the plugins folder. The host
    instantiates it with no arguments, so do real work in :meth:`setup`,
    not in ``__init__``.
    """

    #: Stable, unique, lower-case. Becomes the plugin's content namespace
    #: and its settings key, so changing it resets the user's settings.
    id: str = ""
    #: Shown in the tray menu.
    name: str = ""
    description: str = ""
    version: str = "1.0"
    #: Whether a freshly discovered plugin starts enabled.
    enabled_by_default: bool = True

    @abstractmethod
    def setup(self, ctx: PluginContext) -> None:
        """Called once when the plugin is enabled."""

    def teardown(self) -> None:
        """Called when the plugin is disabled or the island shuts down.

        Timers created through the context are stopped for you. Override
        this only to release something the host does not know about.
        """

    # ---- optional hooks -------------------------------------------------

    def on_island_clicked(self) -> None:
        """Called when the user clicks the island while this plugin owns it."""


def validate(plugin: IslandPlugin) -> str | None:
    """Return a human-readable reason the plugin is unusable, or None."""
    if not isinstance(getattr(plugin, "id", None), str) or not plugin.id.strip():
        return "missing an id"
    if "." in plugin.id:
        # Checked before the general character rule so the message names
        # the real reason: content keys are namespaced with dots, and an id
        # containing one could let a plugin address another provider's
        # content rather than only its own.
        return f"id {plugin.id!r} must not contain a dot"
    if not plugin.id.replace("-", "").replace("_", "").isalnum():
        return f"id {plugin.id!r} must be alphanumeric with - or _"
    if not isinstance(getattr(plugin, "name", None), str) or not plugin.name.strip():
        return "missing a name"
    if not callable(getattr(plugin, "setup", None)):
        return "does not implement setup()"
    return None
