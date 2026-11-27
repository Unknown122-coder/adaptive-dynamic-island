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

### A trap worth naming

A `QRunnable` carries its own signal object, because `QRunnable` is not a
`QObject` and cannot emit anything itself. With `setAutoDelete(True)`, Qt
destroys the runnable as soon as `run()` returns, which can take the signal
object with it before a queued connection has delivered to the GUI thread.
The result is silent: the work completes, the signal is emitted, and
nothing ever arrives.

This cost real debugging time here. The fix is for the owner to hold the
task until its result has been delivered, which both worker paths now do.
The symptom to recognise is a progress state that never resolves while the
work itself demonstrably succeeded.

## Drop to action

The original plan for this feature was fixed drop zones: a left half that
compresses, a right half that converts. That fails on contact with the
actual object, because it asks the user to aim at half of a 30-pixel-tall
pill while already holding a drag, and it cannot grow past two actions.

Taking the drop first and asking afterwards solves both. The whole island
is the target, and the set of offered actions varies with what was
dropped, so a PDF is never offered a WebP conversion.

Two invariants hold in the file layer, and both exist because the input is
someone's only copy of a file:

- Every output is written beside the original under a non-colliding name.
  Nothing is overwritten and nothing is deleted.
- An output larger than its input is discarded and reported as "already
  optimal", rather than written and announced as a saving.

## Plugins

The arbiter made the plugin system almost free. A plugin publishes the
same `IslandContent` every built-in feature publishes, so the window
needed no changes at all to support third-party content, and a plugin
competes for the screen under the same policy as everything else.

Two properties are enforced by the host rather than asked of the author:

- **Priority is clamped.** Everything a plugin publishes is pinned to
  `Priority.PLUGIN`. A plugin asking for critical urgency changes its
  colour, not its ranking, so it cannot bury a privacy warning however it
  is written.
- **Namespaces are assigned.** Content keys are prefixed with the plugin's
  id by the host, and an id containing a dot is rejected at load time,
  because the dot is the namespace separator and an id carrying one could
  address another provider's content.

Timers are handed out by the context and owned by the host, which is the
single most common way a plugin system leaks. Every entry point into
plugin code is wrapped, and three failures disable a plugin for the
session.

What the host cannot do is contain a *hostile* plugin. It is ordinary
Python in the island's process with the user's privileges. The
documentation says so rather than implying a sandbox, because a security
boundary that does not exist is worse than no claim at all.

## System alerts, and the cost of sampling

The sentinel is the one provider that must look at the whole machine, so
it is also the one where the cost of looking matters.

Measured on a real machine:

| Operation | Cost |
|---|---|
| `cpu_percent` + `virtual_memory` | 0.1 ms |
| network interface enumeration | 6 ms |
| full process scan with per-process CPU | 350 ms |

Those numbers dictated the design. The process scan runs only after the
cheap global CPU reading has already said something is wrong, so the
healthy path never pays for it. And all sampling runs on a worker thread,
because 350 ms on the GUI thread would freeze the island's animation for a
third of a second every time the CPU spiked. Only the finished snapshot
crosses back, and the decisions are made on the GUI thread where the
arbiter lives.

Two details that are easy to get wrong:

- **Per-process CPU is relative to one core.** A process can report 400%
  on a four-core machine. Dividing by the core count is what makes the
  figure match the number Task Manager shows, which is the number the
  user will compare it against.
- **Per-process counters need priming.** The first reading after
  attaching to a process is meaningless, so the first scan primes and
  reports nothing. The sustained-condition requirement absorbs that
  naturally.

Network state is read from local interface data and sends no traffic. An
adapter counts as connected only when it is up *and* holds a real IPv4
address: a 169.254 address means the DHCP lease failed, which looks
connected to a naive check and is not.

## The privacy guard, and restraint as a design

The camera feature is the one with the most ways to go wrong, so almost
every decision in it is a decision not to do something.

It does not run unless enabled. It does not import OpenCV until then. It
does not open the camera unless a nominated app is in front or the user
explicitly asked. It does not arm at all if another application is using
the webcam, and disarms if one starts, because taking the camera from a
video call is a worse outcome than missing a shoulder surfer. It does not
act on a single frame. It does not lock the machine by itself. It does not
download its own model.

The two mechanisms worth naming:

**Hysteresis in both directions.** A warning needs several consecutive
agreeing frames, and clearing one needs several too. Without it, a
reflection or a poster interrupts the user once and the feature gets
disabled, at which point it protects nothing. This is the same asymmetry
as credential detection: the expensive failure is the false positive.

**A dismissal cooldown.** Dismissing a warning that is still true would
otherwise bring it straight back, which teaches people to switch the
feature off rather than use it.

Only a face count and a size ratio cross the worker thread boundary. No
frame is retained, written, displayed or transmitted.

### A settings bug worth remembering

Written while building this feature:

```python
seconds = settings.get("privacy.walk_away_seconds", 60) or 60
```

Zero is falsy, so a user setting the value to 0 to switch walk-away
detection off silently got 60 back. Every "this setting does nothing" bug
report starts here. `Settings.number()` and `Settings.integer()` now
handle the fallback properly, and the pattern was removed everywhere it
had been copied to.

## Credential detection

The clipboard guard exists because Windows clipboard history makes a
secret worse rather than better. Detection therefore has to be good enough
that people leave it switched on, and that constraint shapes every rule.

There are two failure modes and they are not symmetric. A **miss** shows a
token on screen, which leaves the user no worse off than Win+V already
does. A **false positive** cries wolf over ordinary text, and after the
second one the feature gets disabled, at which point it protects nothing.
False positives are the expensive failure.

So the rules follow one principle: **prefer an issuer's prefix over a
shape**. `ghp_`, `AKIA`, `xoxb-`, `AIza` and `sk-ant-` are close to
impossible to produce by accident. A rule matching "forty characters of
base64" instead fires on commit hashes, UUIDs, sha256 digests, base64
image data and minified JavaScript, all of which genuinely sit on a
developer's clipboard. Where an issuer offers no prefix, the rule requires
a nearby keyword and an assignment, so `DB_PASSWORD=hunter2` fires and "I
forgot my password again" does not.

Two details that came out of testing rather than design:

- The keyword rule uses a lookbehind rather than `\b`. A word boundary
  does not fire inside `DB_PASSWORD` or `APP_CLIENT_SECRET`, because an
  underscore is a word character, and prefixed names are the single most
  common shape in a `.env` file.
- Values that match a shape but are obviously placeholders, such as
  `changeme`, `<your_api_key>` or `xxxxxxxx`, are filtered out. Flagging
  example configuration teaches users to dismiss the warning, which is
  exactly the habit the feature cannot afford.

A match records a rule id, a severity and the offsets. It never retains
the matched text, and the masked preview is a fixed-width run of dots.
Revealing the last four characters is common practice and is a real leak:
it narrows a brute force and is enough to confirm a guessed credential.
The label already names the service, which is the only thing the user
needs in order to act.

## Keeping the process small

The island is meant to be left running, so the base process never imports
what it is not using. OpenCV and the ONNX runtime together are well over a
hundred megabytes of resident memory and add noticeably to startup, so they
are installed as separate requirement files and imported on first use of
the feature that needs them, not at startup.

Budgets the project holds itself to, and what it currently measures.
`scripts/measure.py` takes these numbers from the real process and exits
non-zero when one is exceeded, so the table cannot quietly drift out of
date.

| Metric | Budget | Measured |
|---|---|---|
| Cold start to visible pill | under 1.5 s | 0.22 s |
| Idle resident memory | under 80 MB | 63.2 MB |
| Idle CPU | under 0.5% | 0.00% |
| Morph animation | 60 fps | not yet instrumented |

Measured on a source checkout with the clock, context engine and activity
server running, sampled over 12 seconds of idle: 15 threads, 402 OS
handles, and 1.1 MB of growth across the sample. A compiled build should
come in lower, because the source checkout carries the interpreter and
uncompiled bytecode.

The idle CPU figure is the point of the event-driven design. A polling
island cannot reach zero, because it is doing work by construction.

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
