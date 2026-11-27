"""A pomodoro timer, as a worked example of an interactive plugin.

Shows the parts of the API a stateful plugin needs: a repeating timer the
host owns, buttons that change what the island offers, and updating one
slot in place so the countdown does not replay its entrance animation
every second.

Copy this file, change the id, and you have your own plugin.
"""

from __future__ import annotations

from core.plugin_api import IslandPlugin, PluginContext

FOCUS_MINUTES = 25
BREAK_MINUTES = 5


class PomodoroPlugin(IslandPlugin):
    id = "pomodoro"
    name = "Pomodoro timer"
    description = "A focus timer with a long-press-free one-click start."
    version = "1.0"
    # Off until the user asks for it: an unrequested timer on screen is
    # clutter, and a sample should not assume it is wanted.
    enabled_by_default = False

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.remaining = 0
        self.running = False
        self.on_break = False
        self.completed = 0

    # ---- lifecycle -----------------------------------------------------

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.focus_minutes = int(ctx.setting("focus_minutes", FOCUS_MINUTES))
        self.break_minutes = int(ctx.setting("break_minutes", BREAK_MINUTES))
        # One timer for the whole plugin, owned by the host.
        ctx.every(1000, self._tick)
        self._show_idle()

    def teardown(self) -> None:
        self.running = False

    # ---- state ---------------------------------------------------------

    def _start(self, minutes: int, on_break: bool) -> None:
        self.remaining = minutes * 60
        self.running = True
        self.on_break = on_break
        self._render()

    def _stop(self) -> None:
        self.running = False
        self._show_idle()

    def _tick(self) -> None:
        if not self.running:
            return
        self.remaining -= 1
        if self.remaining > 0:
            self._render()
            return

        self.running = False
        if self.on_break:
            self._show_idle()
            return
        self.completed += 1
        self._show_finished()

    # ---- presentation ---------------------------------------------------

    def _show_idle(self) -> None:
        assert self.ctx is not None
        done = f"{self.completed} done today" if self.completed else "not started"
        self.ctx.show(
            title="Pomodoro",
            detail=done,
            glyph="clock",
            collapsed="pomodoro",
            actions=(
                self.ctx.action(
                    f"Focus {self.focus_minutes}m",
                    lambda: self._start(self.focus_minutes, False),
                ),
            ),
        )

    def _render(self) -> None:
        assert self.ctx is not None
        minutes, seconds = divmod(max(0, self.remaining), 60)
        clock = f"{minutes:02d}:{seconds:02d}"
        total = (self.break_minutes if self.on_break else self.focus_minutes) * 60
        elapsed = 1.0 - (self.remaining / total) if total else 0.0
        label = "Break" if self.on_break else "Focus"
        self.ctx.show(
            title=f"{label} · {clock}",
            detail=f"{self.completed} completed",
            glyph="clock",
            progress=max(0.0, min(1.0, elapsed)),
            urgency="info" if not self.on_break else "success",
            collapsed=f"{label.lower()} {clock}",
            actions=(self.ctx.action("Stop", self._stop),),
        )

    def _show_finished(self) -> None:
        assert self.ctx is not None
        self.ctx.show(
            title="Focus block finished",
            detail=f"{self.completed} completed · take {self.break_minutes} minutes",
            glyph="sparkle",
            urgency="success",
            collapsed="pomodoro done",
            actions=(
                self.ctx.action(
                    f"Break {self.break_minutes}m",
                    lambda: self._start(self.break_minutes, True),
                ),
                self.ctx.action("Skip", self._show_idle),
            ),
        )
