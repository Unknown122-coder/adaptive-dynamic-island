# Adaptive Dynamic Island for Windows

A context-aware desktop overlay that anchors under the laptop webcam and
changes what it shows based on what you are actually doing.

Windows already has a clipboard history, a text extractor and a task
manager, so this is not another re-implementation of those. The parts that
do not exist anywhere on Windows are the ones this project is built around:

- **A Live Activity API.** Any script, build, test run or download can push
  live progress into the island over a local socket, the way iOS live
  activities work. `island push --id build --progress 0.4` is all it takes.
- **Signal-driven context.** The island reacts to OS events, not to a
  polling loop over process names. Meetings are detected from the Windows
  microphone consent store, so every conferencing app works without being
  on a list.
- **Camera-aware privacy sensing.** Optional shoulder-surfing detection that
  checks whether another app already owns the webcam before it ever opens
  the device.

Everything runs locally. No cloud APIs, no accounts, no telemetry.

## Status

Early development. The window engine, theme system, screen placement and OS
bridge layer are working; the feature providers are being built on top.

## Requirements

- Windows 10 or 11
- Python 3.12 (for running from source)

## Running from source

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

Optional feature packs are installed separately so the base process stays
small:

```
.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
.venv\Scripts\python.exe -m pip install -r requirements-vision.txt
```

## Previewing the UI

Renders every island state to `build/preview/` without needing a desktop
session, which is also how the UI is regression-checked:

```
.venv\Scripts\python.exe scripts/preview.py
```

## License

MIT. See [LICENSE](LICENSE).
