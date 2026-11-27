"""End-to-end check of Drop-to-Action through the running application.

Emits the window's own ``files_dropped`` signal, which is exactly what a
real drag-and-drop produces, then clicks the offered action the same way a
mouse press does: by invoking the callback carried on the content. So this
exercises the real provider, the real arbiter, the real worker thread and
the real clipboard, not a stubbed path.

Run::

    .venv/Scripts/python.exe scripts/e2e_drop.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from main import Application
from scripts.check_media import SAMPLE_TEXT, make_photo_jpeg, make_text_png
from workers import media

failures: list[str] = []
log: list[str] = []


def check(condition: bool, message: str) -> None:
    log.append(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


class DropScenario:
    """Runs the drop steps, waiting on the worker between them."""

    def __init__(self, controller: Application, work: Path) -> None:
        self.controller = controller
        self.work = work
        self.png = make_text_png(work / "screenshot.png")
        self.jpeg = make_photo_jpeg(work / "photo.jpg")
        self.steps = [self.step_ocr, self.step_compress, self.step_rejected]
        self.index = 0

    # ---- helpers -----------------------------------------------------

    def current(self):
        return self.controller.arbiter.current()

    def wait_for(self, predicate, then, timeout_ms: int = 25000) -> None:
        elapsed = {"ms": 0}

        def tick() -> None:
            if predicate():
                then()
                return
            elapsed["ms"] += 100
            if elapsed["ms"] >= timeout_ms:
                check(False, f"timed out waiting for {then.__name__}")
                self.next_step()
                return
            QTimer.singleShot(100, tick)

        QTimer.singleShot(100, tick)

    def click(self, label: str) -> bool:
        """Invoke the action with this label, as a mouse press would."""
        content = self.current()
        if content is None:
            return False
        for action in content.actions:
            if action.label == label:
                action.callback()
                return True
        return False

    def next_step(self) -> None:
        self.index += 1
        if self.index >= len(self.steps):
            self.controller.shutdown()
            return
        QTimer.singleShot(400, self.steps[self.index])

    def start(self) -> None:
        self.steps[0]()

    # ---- steps -------------------------------------------------------

    def step_ocr(self) -> None:
        log.append("\n1. drop a screenshot and extract its text")
        self.controller.window.files_dropped.emit([str(self.png)])

        def after_offer() -> None:
            content = self.current()
            check(
                content is not None and content.key == "drop.offer",
                f"island offers actions (showing {content.key if content else None})",
            )
            if content is None:
                self.next_step()
                return
            labels = [a.label for a in content.actions]
            check("Extract text" in labels, f"offers Extract text, got {labels}")
            check("Compress" in labels, "offers Compress")
            check("To WebP" in labels, "offers To WebP")
            check(content.auto_expand, "the offer opens the island by itself")
            check(
                self.controller.window.is_expanded(),
                "the window actually expanded",
            )
            check(self.click("Extract text"), "clicked Extract text")
            self.wait_for(
                lambda: self.current() is not None
                and self.current().key == "drop.result",
                after_result,
            )

        def after_result() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(content.urgency.name == "SUCCESS", "result reads as a success")
            clipboard = QGuiApplication.clipboard()
            text = clipboard.text() if clipboard else ""
            words = SAMPLE_TEXT.lower().split()
            hits = sum(1 for w in words if w in text.lower())
            check(
                hits >= len(words) - 2,
                f"clipboard holds the recognised text ({hits}/{len(words)} words)",
            )
            check(
                any(a.label == "Copy again" for a in content.actions),
                "offers Copy again",
            )
            self.next_step()

        self.wait_for(
            lambda: self.current() is not None
            and self.current().key == "drop.offer",
            after_offer,
        )

    def step_compress(self) -> None:
        log.append("\n2. drop a photo and compress it")
        before = self.jpeg.stat().st_size
        self.controller.window.files_dropped.emit([str(self.jpeg)])

        def after_offer() -> None:
            check(self.click("Compress"), "clicked Compress")
            self.wait_for(
                lambda: self.current() is not None
                and self.current().key == "drop.result",
                after_result,
            )

        def after_result() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(content.urgency.name == "SUCCESS", "compression succeeded")
            outputs = list(self.work.glob("photo.min*.jpg"))
            check(len(outputs) == 1, f"one new file written, found {outputs}")
            if outputs:
                check(
                    outputs[0].stat().st_size < before,
                    "the new file is smaller than the original",
                )
            check(
                self.jpeg.exists() and self.jpeg.stat().st_size == before,
                "the original photo is untouched",
            )
            check(
                any(a.label == "Show in folder" for a in content.actions),
                "offers Show in folder",
            )
            self.next_step()

        self.wait_for(
            lambda: self.current() is not None
            and self.current().key == "drop.offer",
            after_offer,
        )

    def step_rejected(self) -> None:
        log.append("\n3. drop something with no available action")
        junk = self.work / "archive.zip"
        junk.write_bytes(b"PK\x03\x04nope")
        self.controller.window.files_dropped.emit([str(junk)])

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(
                content.urgency.name == "WARNING",
                "an unsupported drop is reported, not silently ignored",
            )
            check(not content.actions, "no actions offered for it")
            self.next_step()

        self.wait_for(
            lambda: self.current() is not None
            and self.current().key == "drop.result",
            after,
        )


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not media.ocr_available():
        print("SKIPPED: OCR is not installed (pip install -r requirements-ocr.txt)")
        return 0

    controller = Application(app)
    controller.start()

    with tempfile.TemporaryDirectory(prefix="island-drop-") as raw:
        scenario = DropScenario(controller, Path(raw))
        QTimer.singleShot(500, scenario.start)
        QTimer.singleShot(90000, controller.shutdown)
        app.exec()

    print("\n".join(log))
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: drop to action works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
