# Writing a plugin

A plugin is one Python file. Drop it in the plugins folder, rescan from
the tray, and it appears in the menu.

```
%APPDATA%\AdaptiveIsland\plugins\
```

The island also scans the `plugins/` folder next to the code, where the
two samples live. A user plugin with the same id as a shipped one wins, so
you can replace a sample without touching the install directory.

## The smallest plugin that does something

```python
from core.plugin_api import IslandPlugin, PluginContext


class Hello(IslandPlugin):
    id = "hello"
    name = "Hello"
    description = "Says hello."

    def setup(self, ctx: PluginContext) -> None:
        ctx.show("Hello", detail="from a plugin", glyph="sparkle")
```

That is the whole contract. The island instantiates your class with no
arguments, calls `setup` once with a context, and calls `teardown` when
the plugin is switched off or the island exits.

Do real work in `setup`, not in `__init__`. A plugin is constructed during
discovery even if the user never enables it.

## The context

`ctx` is everything you get. There is no access to the window, the
arbiter, or other plugins.

| Call | What it does |
|---|---|
| `ctx.show(title, ...)` | Put something on the island |
| `ctx.hide(slot="main")` | Remove it |
| `ctx.action(label, fn)` | Build a button for `show` |
| `ctx.every(ms, fn)` | Repeating timer, stopped for you on unload |
| `ctx.after(ms, fn)` | One-shot timer, also owned by the host |
| `ctx.setting(key, default)` | Read your own settings block |
| `ctx.log(message)` | Write to the island's log |

`show` accepts `title`, `detail`, `glyph`, `progress`, `urgency`,
`actions`, `collapsed`, `ttl`, `accent` and `slot`.

- `progress` is `0.0` to `1.0`, or a negative number for an indeterminate
  bar. Omit it for no bar.
- `urgency` is `neutral`, `info`, `success`, `warning` or `critical` and
  sets the accent colour.
- `accent` overrides that with an exact `#RRGGBB`, for when the colour is
  the content.
- `ttl` is seconds before the island drops it on its own.
- `slot` lets one plugin publish more than one thing. Calling `show` again
  with the same slot updates in place, which is what you want for a
  counter: it does not replay the entrance animation.

Glyph names are listed in [live-activities.md](live-activities.md).

## Timers

Use `ctx.every` rather than your own `QTimer`. The host owns the timers it
hands out and stops them when your plugin is unloaded, which is the usual
way a plugin system leaks. A callback that raises is caught and logged
rather than propagating into the island's event loop.

## Settings

Anything under your plugin's id in the settings file is available through
`ctx.setting`:

```json
{
  "plugins": {
    "pomodoro": { "enabled": true, "focus_minutes": 50 }
  }
}
```

```python
minutes = int(ctx.setting("focus_minutes", 25))
```

The `enabled` key is managed by the tray menu. Settings are read when your
plugin is activated, so a change takes effect on the next rescan.

## What the host guarantees

**You cannot outrank a warning.** Everything a plugin publishes is clamped
to plugin priority, below live activities, system alerts and privacy
warnings. Asking for `urgency="critical"` changes the colour, not the
ranking. This is enforced, not a convention.

**You cannot take the island down.** Every call into plugin code is
wrapped. A plugin that raises is reported in the log and the tray; after
three failures it is switched off for the session and the island carries
on.

**Your namespace is yours.** Content keys are prefixed with your id, so
one plugin cannot retract another's content. Ids containing a dot are
rejected at load time for the same reason.

## What the host does not guarantee

There is no sandbox. A plugin is ordinary Python running inside the
island's process with your full privileges. It can read your files, open
sockets, and run anything Python can run.

Installing a plugin is exactly as much of a decision as running any other
script you downloaded. Read it first. The island can contain a *buggy*
plugin; it cannot contain a hostile one, and claiming otherwise would be
worse than saying so plainly.

## Two worked examples

Both ship in `plugins/` and are off by default.

**`sample_pomodoro.py`** is the interactive shape: state, a repeating
timer, buttons that change what the island offers, and one slot updated
in place every second.

**`sample_battery.py`** is the sensor shape, and the more useful pattern.
It watches something and publishes nothing until it matters. A plugin that
shows a battery percentage all day is noise, because the taskbar already
has one; this one appears below a threshold and retracts when the charger
goes in.

## Debugging

Run the island from a terminal and you will see import errors, failures
and `ctx.log` output:

```
.venv\Scripts\python.exe main.py
```

The tray's Diagnostics entry lists every discovered plugin, its state, the
file it came from, and the reason anything was rejected.
