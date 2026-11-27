"""End-to-end check of the clipboard guard against the real clipboard.

Writes to the actual Windows clipboard and lets the application react
through its own signal path, so this exercises the real listener, the
real scanner and the real arbiter.

It restores whatever was on the clipboard beforehand, because running a
test should not cost the user their copy.

Run::

    .venv/Scripts/python.exe scripts/e2e_clipboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from main import Application

# Fabricated, in the documented shape of a real token, and assembled at
# runtime so the source file contains nothing a credential scanner would
# flag. See the note in tests/test_secrets.py.
FAKE_JWT = "".join(
    (
        "ey", "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ".ey", "JzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    )
)
FAKE_AWS = "".join(("AK", "IA", "IOSFODNN7EXAMPLE"))

failures: list[str] = []
log: list[str] = []


def check(condition: bool, message: str) -> None:
    log.append(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


class Scenario:
    def __init__(self, controller: Application) -> None:
        self.controller = controller
        self.steps = [
            self.step_colour,
            self.step_json,
            self.step_secret,
            self.step_wipe,
            self.step_stack_excludes_secret,
        ]
        self.index = 0

    # ---- helpers -----------------------------------------------------

    def current(self):
        return self.controller.arbiter.current()

    def copy(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(text)

    def wait_for(self, predicate, then, timeout_ms: int = 8000) -> None:
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

    def showing(self, key: str) -> bool:
        content = self.current()
        return content is not None and content.key == key

    def click(self, label: str) -> bool:
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
        QTimer.singleShot(300, self.steps[self.index])

    def start(self) -> None:
        self.steps[0]()

    # ---- steps -------------------------------------------------------

    def step_colour(self) -> None:
        log.append("\n1. copy a hex colour")
        self.copy("#5B8CFF")

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(content.title == "#5B8CFF", "shows the normalised hex value")
            check(
                content.accent == "#5B8CFF",
                f"carries the colour as its accent (got {content.accent})",
            )
            check("blue" in content.detail, "names the colour")
            self.next_step()

        self.wait_for(lambda: self.showing("clipboard.copied"), after)

    def step_json(self) -> None:
        log.append("\n2. copy malformed JSON")
        self.copy('{"user": "akmal", "roles": ["admin",')

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(content.title == "Invalid JSON", "flags the broken paste")
            check(content.glyph == "warning", "uses a warning glyph")
            self.next_step()

        self.wait_for(
            lambda: self.showing("clipboard.copied")
            and self.current().title != "#5B8CFF",
            after,
        )

    def step_secret(self) -> None:
        log.append("\n3. copy something that looks like a credential")
        self.copy(FAKE_JWT)

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(
                content.priority.name == "ALERT",
                "a credential outranks ordinary content",
            )
            check("JSON web token" in content.title, "names the credential type")
            # The whole point: the value must never reach the screen.
            rendered = f"{content.title} {content.detail} {content.collapsed_text}"
            check(FAKE_JWT not in rendered, "the token itself is never displayed")
            check(
                FAKE_JWT[:20] not in rendered,
                "not even a prefix of the token is displayed",
            )
            check(content.auto_expand, "the warning opens the island by itself")
            check(
                any(a.label == "Wipe now" for a in content.actions),
                "offers to wipe the clipboard",
            )
            self.next_step()

        self.wait_for(lambda: self.showing("clipboard.secret"), after)

    def step_wipe(self) -> None:
        log.append("\n4. wipe the clipboard")
        self.copy(FAKE_AWS)

        def after_warning() -> None:
            check(self.click("Wipe now"), "clicked Wipe now")
            QTimer.singleShot(300, after_wipe)

        def after_wipe() -> None:
            clipboard = QGuiApplication.clipboard()
            remaining = clipboard.text()
            check(not remaining, f"clipboard is empty (got {remaining[:20]!r})")
            content = self.current()
            log.append(f"  -> {content.title} · {content.detail}")
            check(
                content.title == "Clipboard cleared",
                "the island confirms the wipe",
            )
            self.next_step()

        self.wait_for(lambda: self.showing("clipboard.secret"), after_warning)

    def step_stack_excludes_secret(self) -> None:
        log.append("\n5. the stack must not remember credentials")
        entries = self.controller.clipboard.entries()
        texts = [e.text for e in entries]
        log.append(f"  -> stack holds {len(entries)}: {[t[:24] for t in texts]}")
        check(
            FAKE_JWT not in texts and FAKE_AWS not in texts,
            "no credential was ever added to the stack",
        )
        check(
            any(t == "#5B8CFF" for t in texts),
            "ordinary copies are remembered",
        )
        self.next_step()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    clipboard = QGuiApplication.clipboard()
    original = clipboard.text()

    controller = Application(app)
    # The guard is opt-in; this test is about it, so turn it on.
    controller.settings.set("providers.clipboard", True)
    controller.start()
    controller.clipboard.start()
    check(controller.clipboard.running, "the clipboard guard started")

    scenario = Scenario(controller)
    QTimer.singleShot(500, scenario.start)
    QTimer.singleShot(60000, controller.shutdown)
    app.exec()

    # Put the user's clipboard back the way it was.
    if original:
        clipboard.setText(original)

    print("\n".join(log))
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: clipboard guard works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
