# Architecture

## The problem with the obvious design

The straightforward way to build a desktop island is to give each feature
its own widget and let each one decide when to show itself. That design
collapses the moment two features want the strip at the same time. A clock
is drawn over a build-failure warning because its timer happened to fire
last; motion is inconsistent because every widget animates its own way;
and adding a seventh feature means touching the other six.

This project separates *what to say* from *how to show it*.

```
 providers                      arbitration                   presentation
┌────────────────────┐         ┌──────────────┐         ┌───────────────────┐
│ ambient clock      │         │              │         │                   │
│ context engine     │ publish │   Content    │ single  │   IslandWindow    │
│ live activity API  ├────────►│   Arbiter    ├────────►│  one paintEvent   │
│ system sentinel    │Island   │  priority +  │ content │  one morph path   │
│ privacy guard      │Content  │  expiry      │ signal  │                   │
│ user plugins       │         │              │         │                   │
└────────────────────┘         └──────────────┘         └───────────────────┘
```

Every provider emits the same type, `IslandContent`. The arbiter picks one
winner. The window renders whatever it is given and knows nothing about
Git, webcams or CPU load.

Three consequences follow, and they are the reason for the design:

1. Adding a feature cannot break the animation, because features do not
   animate. There is exactly one animation path.
2. Conflicts are resolved by stated policy rather than by timer order.
3. A provider can be switched off at runtime without the window caring.

## Arbitration policy

Priority is an enum, ordered so that protecting the user always outranks
informing them:

| Priority | Value | Used by |
|---|---|---|
| `PRIVACY` | 100 | Shoulder-surfing and camera warnings |
| `ALERT` | 80 | Degraded system or network state |
| `ACTIVITY` | 60 | Live activities: builds, tests, downloads |
| `PLUGIN` | 40 | Third-party widgets |
| `CONTEXT` | 30 | App profiles: developer, meeting, browser |
| `AMBIENT` | 10 | Clock, idle chrome |

Ties are broken by publication order, not by timestamp. `time.monotonic()`
on Windows advances in roughly 15 ms steps, so two providers publishing in
the same tick compare equal and the winner would otherwise depend on
dictionary ordering. A counter on every content object makes the ordering
total.

Content carries an optional `expires_at`. The arbiter sweeps expired items
on a single shared timer, which is what stops a producer that crashed
mid-build from pinning a progress bar on screen forever.

## Why the OS integration is event-driven

Most Windows island projects poll `GetForegroundWindow` two to four times a
second. This one installs `SetWinEventHook` with `WINEVENT_OUTOFCONTEXT`,
which posts events to the thread's message queue. Qt pumps that queue on the
GUI thread, so hook callbacks arrive inside the normal event loop with no
extra thread and no polling. Idle cost is zero and latency is the OS's own.

A slow safety timer re-reads the foreground window every few seconds,
because hooks do occasionally get dropped, and a frozen idea of context is
worse than a cheap re-check.

| Signal | Source | Cost at idle |
|---|---|---|
| Active window and title | `SetWinEventHook` | none |
| Microphone or camera in use | `RegNotifyChangeKeyValue` on the consent store | none |
| Git branch | `.git/HEAD`, read directly | none |
| Dirty file count | `git status` on a worker thread, cached 5 s | none |
| Clock | one timer, realigned to the next minute | 1 wake per minute |

## Meetings without an application allowlist

Windows records every microphone and camera access under
`HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore`.
Each app subkey holds `LastUsedTimeStart` and `LastUsedTimeStop`; while an
app holds the device, the start time is set and the stop time is zero. That
is the signal behind the privacy indicator Windows itself shows in the tray.

Reading it gives three things a process-name allowlist cannot:

- **Meeting detection that does not enumerate vendors.** Teams, Zoom,
  Discord, Slack huddles and a Google Meet tab in any browser all light up
  the same key, including when the user is in a call while looking at a
  different window.
- **Camera arbitration.** The privacy guard checks whether another app
  already owns the webcam before opening it, so the feature can never fight
  a video call for the device.
- **An honest privacy story.** The app reads a usage ledger the OS already
  keeps. It never inspects audio or video content to decide this.

## Anchoring to the right screen

The island is only meaningful on the panel that physically has a webcam
above it. On a docked laptop the Windows *primary* screen is usually the
external monitor, so "primary" is the wrong default.

`QueryDisplayConfig` reports an output technology per active display path,
and `DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL` marks the built-in panel.
That path resolves to a GDI device name, which is what `QScreen.name()`
returns, so the result maps back onto a Qt screen. Every failure path falls
back to the primary screen rather than refusing to start.

## Threading

The UI thread owns the window, the arbiter and every hook. Anything that can
block runs elsewhere:

| Work | Where it runs |
|---|---|
| `git status` | `QThreadPool` task, result delivered by signal |
| Registry change waits | dedicated `QThread` blocking on `WaitForMultipleObjects` |
| OCR and image work | worker thread, lazily imported on first use |
| Webcam sampling | worker thread, opt-in only |

A late result is dropped rather than displayed: if the user has alt-tabbed
away before a Git probe finishes, showing that branch would be wrong.

## Keeping the process small

The island is meant to be left running, so the base process never imports
what it is not using. OpenCV and the ONNX runtime together are well over a
hundred megabytes of resident memory and add noticeably to startup, so they
are installed as separate requirement files and imported on first use of
the feature that needs them, not at startup.

Budgets the project holds itself to:

| Metric | Budget |
|---|---|
| Idle resident memory | under 80 MB |
| Idle CPU | under 0.5% |
| Cold start to visible pill | under 1.5 s |
| Morph animation | 60 fps, no dropped frames |

## Rendering

The window is a single `paintEvent` rather than a tree of child widgets. At
this size a widget tree costs more than it gives: the island redraws
continuously during a morph, needs pixel control over a rounded translucent
shape, and benefits from a flat widget count that keeps idle memory stable.

Icons are vector paths, not an icon font. Segoe Fluent Icons ships only on
Windows 11; Windows 10 has Segoe MDL2 Assets with different codepoints.
Drawing twenty icons with `QPainterPath` avoids maintaining two codepoint
tables, renders identically everywhere, and takes its colour from the theme.

No timer runs while the island is at rest. The indeterminate-progress
shimmer exists only while indeterminate progress is on screen.

## Not stealing focus

An overlay that takes the caret while someone is typing is worse than no
overlay. The window sets `WS_EX_NOACTIVATE` and `WS_EX_TOOLWINDOW` directly
rather than relying on Qt flags alone, and becomes click-through
(`WS_EX_TRANSPARENT`) whenever it is showing something with nothing to
click, so a collapsed pill never swallows a click aimed at a browser tab
behind it.

Windows also demotes topmost windows after UAC prompts and full-screen
transitions, so the z-order is re-asserted on a cheap schedule.
