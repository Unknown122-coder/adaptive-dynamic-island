"""Start the real application, let it run briefly, then report and exit.

This is the check that the whole thing actually assembles on a live
desktop: hooks install, the consent watcher reads the registry, the
arbiter publishes, the window maps on the right screen. It exits non-zero
if anything a user would notice is wrong, so it can gate a commit.

Run::

    .venv/Scripts/python.exe scripts/smoke.py [seconds]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from main import Application  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    controller = Application(app)
    controller.start()

    problems: list[str] = []

    def inspect() -> None:
        window = controller.window
        placement = window.placement()
        content = controller.arbiter.current()

        print("--- smoke ---")
        print(controller._diagnostics_text())  # noqa: SLF001 - diagnostic tool
        print("-------------")
        print(f"window visible : {window.isVisible()}")
        print(f"window geometry: {window.geometry()}")
        print(f"showing        : {content.key if content else 'nothing'}")

        if placement is None:
            problems.append("no screen placement resolved")
        if not controller.foreground.hooked:
            problems.append("SetWinEventHook did not install")
        if content is None:
            problems.append("arbiter published no content")
        if not window.isVisible():
            problems.append("island window never became visible")
        elif window.geometry().width() <= 0:
            problems.append("island window has no size")

        controller.shutdown()

    QTimer.singleShot(int(seconds * 1000), inspect)
    app.exec()

    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: island assembled, anchored and rendering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
