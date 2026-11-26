"""End-to-end check of the Live Activity API.

Starts the real application in-process, then drives the real ``island``
command line client as a separate process, exactly as a build script
would, and asserts that each command reaches the island and changes what
it is showing.

The client has to run as a separate process: its socket calls block, and
blocking the GUI thread would stop the server from ever accepting the
connection. QProcess keeps the event loop spinning while it runs.

Run::

    .venv/Scripts/python.exe scripts/e2e_activity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QProcess, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from main import Application  # noqa: E402
from core.model import Priority  # noqa: E402

PYTHON = sys.executable
CLI = str(ROOT / "island.py")

failures: list[str] = []
log: list[str] = []


def check(condition: bool, message: str) -> None:
    mark = "ok  " if condition else "FAIL"
    log.append(f"  [{mark}] {message}")
    if not condition:
        failures.append(message)


class Runner:
    """Runs a list of (description, argv, verifier) steps in order."""

    def __init__(self, controller: Application, steps: list) -> None:
        self.controller = controller
        self.steps = steps
        self.index = 0
        self.process: QProcess | None = None

    def start(self) -> None:
        self._next()

    def _next(self) -> None:
        if self.index >= len(self.steps):
            self.finish()
            return
        description, argv, verifier = self.steps[self.index]
        self.index += 1
        log.append(f"\n$ island {' '.join(argv)}   ({description})")

        process = QProcess()
        process.setProgram(PYTHON)
        process.setArguments([CLI, *argv])
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process = process

        def done(code: int, _status) -> None:
            output = bytes(process.readAll()).decode("utf-8", "replace").strip()
            if output:
                for line in output.splitlines():
                    log.append(f"  > {line}")
            check(code == 0, f"{description}: client exited 0 (got {code})")
            # Let the server's signal reach the arbiter before inspecting.
            QTimer.singleShot(250, lambda: self._verify(description, verifier))

        process.finished.connect(done)
        process.start()

    def _verify(self, description: str, verifier) -> None:
        if verifier is not None:
            verifier(self.controller, description)
        QTimer.singleShot(50, self._next)

    def finish(self) -> None:
        self.controller.shutdown()


def verify_showing_activity(controller: Application, description: str) -> None:
    content = controller.arbiter.current()
    check(content is not None, f"{description}: island is showing something")
    if content is None:
        return
    check(
        content.key == "activity.demo",
        f"{description}: showing activity.demo (got {content.key})",
    )
    check(
        content.priority == Priority.ACTIVITY,
        f"{description}: ranked as an activity",
    )
    check(
        abs((content.progress or 0) - 0.35) < 0.01,
        f"{description}: progress is 0.35 (got {content.progress})",
    )
    check(
        content.collapsed_text.endswith("35%"),
        f"{description}: collapsed label shows a percentage "
        f"(got {content.collapsed_text!r})",
    )
    check(
        controller.window.isVisible(),
        f"{description}: window is on screen",
    )


def verify_outranks_clock(controller: Application, description: str) -> None:
    keys = [c.key for c in controller.arbiter.candidates()]
    check(
        "ambient.clock" in keys,
        f"{description}: the clock is still a candidate",
    )
    current = controller.arbiter.current()
    check(
        current is not None and current.key == "activity.demo",
        f"{description}: the activity outranks the clock",
    )


def verify_finished(controller: Application, description: str) -> None:
    content = controller.arbiter.current()
    check(content is not None, f"{description}: island still showing something")
    if content is None:
        return
    check(
        content.key == "activity.demo",
        f"{description}: completion replaced the activity in place",
    )
    check(
        "built in 42s" in (content.detail or ""),
        f"{description}: completion detail is shown (got {content.detail!r})",
    )
    check(
        content.expires_at is not None,
        f"{description}: completed activity is set to expire",
    )


def verify_retracted(controller: Application, description: str) -> None:
    keys = [c.key for c in controller.arbiter.candidates()]
    check(
        "activity.demo" not in keys,
        f"{description}: activity.demo is gone (candidates: {keys})",
    )
    current = controller.arbiter.current()
    check(
        current is not None and current.key == "ambient.clock",
        f"{description}: the island fell back to the clock",
    )


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    controller = Application(app)
    controller.start()

    if not controller.activities.listening:
        print("FAILED: the activity server is not listening")
        return 1
    print(f"activity API listening on port {controller.activities.port}")

    steps = [
        ("ping", ["ping"], None),
        (
            "push an activity",
            [
                "push",
                "demo",
                "docker build · api",
                "--progress",
                "0.35",
                "--detail",
                "step 5 of 14",
                "--glyph",
                "cube",
            ],
            verify_showing_activity,
        ),
        ("priority over ambient", ["list"], verify_outranks_clock),
        (
            "complete the activity",
            ["done", "demo", "--status", "ok", "--detail", "built in 42s"],
            verify_finished,
        ),
        ("retract it", ["retract", "demo"], verify_retracted),
    ]

    runner = Runner(controller, steps)
    QTimer.singleShot(600, runner.start)
    # Hard stop so a hung client cannot hang the test run.
    QTimer.singleShot(45000, controller.shutdown)
    app.exec()

    print("\n".join(log))
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: live activity API works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
