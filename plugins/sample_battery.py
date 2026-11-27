"""Battery warnings, as a worked example of a sensor plugin.

The counterpart to the pomodoro sample. That one is interactive and
stateful; this one watches something and stays silent until it matters,
which is the shape most useful plugins take.

The interesting part is the silence. A plugin that shows a battery
percentage all day is noise, because the taskbar already has one. This
publishes nothing until the battery crosses a threshold, and retracts as
soon as it recovers or the charger goes in.
"""

from __future__ import annotations

import psutil

from core.plugin_api import IslandPlugin, PluginContext

LOW_PERCENT = 20
CRITICAL_PERCENT = 10
POLL_SECONDS = 30


class BatteryPlugin(IslandPlugin):
    id = "battery"
    name = "Battery warnings"
    description = "Warns on low battery and confirms when the charger goes in."
    version = "1.0"
    enabled_by_default = False

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.was_plugged: bool | None = None

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.low = int(ctx.setting("low_percent", LOW_PERCENT))
        self.critical = int(ctx.setting("critical_percent", CRITICAL_PERCENT))
        ctx.every(int(ctx.setting("poll_seconds", POLL_SECONDS)) * 1000, self._check)
        self._check()

    def _check(self) -> None:
        assert self.ctx is not None
        battery = psutil.sensors_battery()
        if battery is None:
            # A desktop machine. Nothing to say, ever.
            self.ctx.hide()
            return

        percent = int(battery.percent)
        plugged = bool(battery.power_plugged)

        # Confirm a state change briefly, then go quiet again.
        if self.was_plugged is not None and plugged != self.was_plugged:
            self.ctx.show(
                title="Charging" if plugged else "On battery",
                detail=f"{percent}%",
                glyph="pulse",
                urgency="success" if plugged else "neutral",
                collapsed=f"{'charging' if plugged else 'battery'} {percent}%",
                ttl=4,
            )
            self.was_plugged = plugged
            return
        self.was_plugged = plugged

        if plugged or percent > self.low:
            self.ctx.hide()
            return

        critical = percent <= self.critical
        self.ctx.show(
            title=f"Battery at {percent}%",
            detail="plug in now" if critical else "running low",
            glyph="warning",
            progress=percent / 100.0,
            urgency="critical" if critical else "warning",
            collapsed=f"battery {percent}%",
        )
