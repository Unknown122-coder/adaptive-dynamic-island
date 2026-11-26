# Live Activity API

The island accepts live updates from any program on the machine. This is
the part iOS's Dynamic Island is actually for, and the part every Windows
clone leaves out.

A build, test run, deploy, download or long transfer pushes updates under a
stable id; the island shows it, ranks it against everything else, and drops
it when it finishes or when the producer goes quiet.

## Quick start

With the island running:

```
island ping
island push build "docker build · api" --progress 0.4 --detail "step 7 of 14"
island done build --status ok --detail "built in 42s"
```

Or wrap any command and let it report itself:

```
island run --id tests --title "pytest" -- pytest -q
```

`island run` shows the command while it runs, then reports its exit status
and duration. The wrapped tool needs to know nothing about the island.

## Transport

Newline-delimited JSON over a loopback TCP socket. One JSON object per
line, one response object per line.

A named pipe would be marginally more Windows-native, but JSON over TCP can
be driven from Python, Node, Go, PowerShell or curl without a binding, and
the entire point of this API is that anything a developer already runs can
report into it.

The server binds a free port on `127.0.0.1` at startup and writes the port
and a freshly generated token to:

```
%APPDATA%\AdaptiveIsland\endpoint.json
```

Every command must carry that token. This stops another user's session on
the same machine from pushing content, while keeping the barrier low enough
that a build script can use it in one line.

## Commands

| Op | Required | Optional |
|---|---|---|
| `push` | `id`, `title` | `detail`, `progress`, `glyph`, `urgency`, `ttl` |
| `done` | `id` | `status`, `title`, `detail` |
| `retract` | `id` | |
| `list` | | |
| `ping` | | |

- `progress` is `0.0` to `1.0`, or any negative number for an
  indeterminate bar. Omit it for no bar at all.
- `urgency` is one of `neutral`, `info`, `success`, `warning`, `critical`
  and sets the accent colour.
- `status` on `done` is `ok`, `fail` or `cancelled`.
- `ttl` is how many seconds the island keeps the activity without a further
  update. It defaults to 90 seconds.

Pushing the same `id` again updates in place: the island does not replay
its entrance animation, so a progress bar ticking up does not strobe.

## Reliability

The producers are other people's scripts, so the server assumes they
misbehave:

- **A crashed producer cannot pin content on screen.** Every push carries a
  time-to-live and the arbiter sweeps expired content automatically.
- **A hung client cannot stall the island.** Lines are capped and the
  server never blocks the UI thread.
- **A producer cannot impersonate another feature.** Ids are namespaced
  server-side and the separator is stripped from incoming ids, so an
  activity cannot claim to be a privacy warning.
- **Text is sanitised.** Control characters and newlines are removed, and
  fields are length-capped.

## From Python

The client module is pure standard library. A build script reporting
progress should not have to import Qt:

```python
from ipc.client import activity

with activity("build", "docker build · api", glyph="cube") as act:
    for step, total in steps():
        act.update(progress=step / total, detail=f"step {step} of {total}")
```

The context manager reports success on a clean exit, `cancelled` on
Ctrl-C, and `fail` if the block raises, so a crashed build shows as failed
rather than sitting on screen until its time-to-live expires. If the island
is not running, every call is a no-op: a producer must never fail its own
build because an overlay is closed.

## From anywhere else

Read the endpoint file, open a socket, write a line. In PowerShell:

```powershell
$e = Get-Content "$env:APPDATA\AdaptiveIsland\endpoint.json" | ConvertFrom-Json
$client = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $e.port)
$stream = $client.GetStream()
$payload = @{ op='push'; token=$e.token; id='deploy'; title='deploying api'
              progress=-1 } | ConvertTo-Json -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($payload + "`n")
$stream.Write($bytes, 0, $bytes.Length)
$client.Close()
```

## Glyphs

Pass any of these names as `glyph`:

`branch`, `commit`, `mic`, `mic-off`, `camera`, `eye`, `shield`,
`shield-alert`, `faces`, `lock`, `cpu`, `pulse`, `warning`, `wifi-off`,
`flask`, `cube`, `download`, `clipboard`, `key`, `text`, `clock`,
`sparkle`.

An unknown name simply renders without an icon rather than failing.
