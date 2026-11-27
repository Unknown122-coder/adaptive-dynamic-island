"""End-to-end check of the plugin system in the running application.

Loads the shipped pomodoro sample, clicks its buttons the way a user
would, and then checks the two containment guarantees against the live
arbiter rather than in isolation: that a plugin cannot outrank a system
alert, and that a plugin written badly enough to raise cannot take the
island down with it.

Run::

    .venv/Scripts/python.exe scripts/e2e_plugins.py
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from core.model import IslandContent, Priority, Urgency
from main import Application

failures: list[str] = []
log: list[str] = []


def check(condition: bool, message: str) -> None:
    log.append(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


class Scenario:
    def __init__(self, controller: Application, scratch: Path) -> None:
        self.controller = controller
        self.scratch = scratch
        self.steps = [
            self.step_discovery,
            self.step_activate,
            self.step_interact,
            self.step_priority_cap,
            self.step_deactivate,
            self.step_bad_plugin,
            self.step_toggle_isolation,
        ]
        self.index = 0

    # ---- helpers -----------------------------------------------------

    def current(self):
        return self.controller.arbiter.current()

    def click(self, prefix: str) -> bool:
        content = self.current()
        if content is None:
            return False
        for action in content.actions:
            if action.label.startswith(prefix):
                action.callback()
                return True
        return False

    def wait_for(self, predicate, then, timeout_ms: int = 10000) -> None:
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

    def next_step(self) -> None:
        self.index += 1
        if self.index >= len(self.steps):
            self.controller.shutdown()
            return
        QTimer.singleShot(300, self.steps[self.index])

    def start(self) -> None:
        self.steps[0]()

    # ---- steps -------------------------------------------------------

    def step_discovery(self) -> None:
        log.append("\n1. discover the shipped samples")
        manager = self.controller.plugins
        ids = [entry.id for entry in manager.all()]
        log.append(f"  -> found {ids}")
        check("pomodoro" in ids, "pomodoro sample discovered")
        check("battery" in ids, "battery sample discovered")
        check(
            manager.report().rejected == [],
            f"nothing rejected (got {manager.report().rejected})",
        )
        check(
            manager.active_ids() == [],
            "samples stay off until asked for",
        )
        self.next_step()

    def step_activate(self) -> None:
        log.append("\n2. turn the pomodoro plugin on")
        self.controller.plugins.set_enabled("pomodoro", True)

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.key}: {content.title} · {content.detail}")
            check(
                content.key.startswith("plugin.pomodoro."),
                f"publishes into its own namespace ({content.key})",
            )
            check(content.title == "Pomodoro", "shows its idle state")
            self.next_step()

        self.wait_for(
            lambda: self.current() is not None
            and self.current().key.startswith("plugin.pomodoro."),
            after,
        )

    def step_interact(self) -> None:
        log.append("\n3. start a focus block")
        check(self.click("Focus"), "clicked the Focus button")

        def after() -> None:
            content = self.current()
            log.append(f"  -> {content.title} · progress {content.progress}")
            check(content.title.startswith("Focus"), "the countdown is running")
            check(
                content.progress is not None and content.progress >= 0,
                "it shows determinate progress",
            )
            check(
                any(a.label == "Stop" for a in content.actions),
                "the buttons changed to offer Stop",
            )
            self.first_clock = content.title
            QTimer.singleShot(1400, after_tick)

        def after_tick() -> None:
            content = self.current()
            log.append(f"  -> a second later: {content.title}")
            check(
                content.title != self.first_clock,
                "the clock advanced, so the plugin's timer is live",
            )
            self.next_step()

        self.wait_for(
            lambda: self.current() is not None
            and self.current().title.startswith("Focus"),
            after,
        )

    def step_priority_cap(self) -> None:
        log.append("\n4. a system alert must still win")
        before = self.current()
        check(
            before.priority == Priority.PLUGIN,
            f"plugin content sits at PLUGIN priority (got {before.priority!r})",
        )
        # Stand in for the system sentinel.
        self.controller.arbiter.publish(
            IslandContent(
                key="alert.test",
                priority=Priority.ALERT,
                title="Simulated system alert",
                urgency=Urgency.WARNING,
            )
        )

        def after() -> None:
            content = self.current()
            log.append(f"  -> island now shows {content.key}")
            check(
                content.key == "alert.test",
                "the alert outranks the plugin",
            )
            self.controller.arbiter.retract("alert.test")
            QTimer.singleShot(200, after_retract)

        def after_retract() -> None:
            content = self.current()
            check(
                content is not None and content.key.startswith("plugin.pomodoro."),
                "the plugin returns once the alert clears",
            )
            self.next_step()

        QTimer.singleShot(300, after)

    def step_deactivate(self) -> None:
        log.append("\n5. turn it off again")
        self.controller.plugins.set_enabled("pomodoro", False)

        def after() -> None:
            content = self.current()
            key = content.key if content else "nothing"
            log.append(f"  -> island now shows {key}")
            check(
                not key.startswith("plugin.pomodoro."),
                "its content was retracted",
            )
            check(
                self.controller.plugins.active_ids() == [],
                "no plugins remain active",
            )
            self.next_step()

        QTimer.singleShot(400, after)

    def step_bad_plugin(self) -> None:
        log.append("\n6. a badly written plugin must not take the island down")
        (self.scratch / "explode.py").write_text(
            textwrap.dedent(
                """
                from core.plugin_api import IslandPlugin

                class Explode(IslandPlugin):
                    id = "explode"
                    name = "Explode"
                    enabled_by_default = True
                    def setup(self, ctx):
                        raise RuntimeError("this plugin is broken")
                """
            ),
            encoding="utf-8",
        )
        (self.scratch / "syntax.py").write_text(
            "def broken(:\n", encoding="utf-8"
        )

        manager = self.controller.plugins
        report = manager.discover([self.scratch, ROOT / "plugins"])
        log.append(f"  -> discovery: {report.summary()}")
        check("explode" in report.loaded, "the raising plugin was still loaded")
        check(
            any("syntax.py" in name for name, _ in report.rejected),
            "the unparsable file was rejected, not fatal",
        )

        manager.start_enabled()
        entry = manager.get("explode")
        check(entry is not None and not entry.active, "it failed to activate")
        check(
            self.controller.window.isVisible(),
            "the island is still running",
        )
        content = self.current()
        log.append(f"  -> island still showing {content.key if content else None}")
        check(content is not None, "the island still has content to show")
        self.next_step()


    def step_toggle_isolation(self) -> None:
        """Toggling one feature must not disturb another.

        This exists because of a real bug: an edit landed a statement at
        the wrong indentation in the provider-toggle ladder, so switching
        any feature on or off also unloaded every plugin. It parsed, the
        unit tests passed, and the plugin end-to-end test passed too,
        because none of them went through the tray's code path.
        """
        log.append("\n7. toggling one feature leaves the others alone")
        manager = self.controller.plugins
        manager.discover([ROOT / "plugins"])
        manager.set_enabled("pomodoro", True)
        check(manager.active_ids() == ["pomodoro"], "pomodoro is active")

        for feature in ("clipboard", "clock", "context", "sentinel"):
            self.controller._on_provider_toggled(feature, True)
            self.controller._on_provider_toggled(feature, False)
        check(
            manager.active_ids() == ["pomodoro"],
            f"plugins survived toggling other features "
            f"(active: {manager.active_ids()})",
        )

        self.controller._on_provider_toggled("plugins", False)
        check(
            manager.active_ids() == [],
            "toggling plugins off does unload them",
        )
        check(
            self.controller.privacy.state().enabled is False,
            "and did not leave the privacy guard running",
        )
        self.next_step()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    controller = Application(app)
    controller.start()

    with tempfile.TemporaryDirectory(prefix="island-plugins-") as raw:
        scenario = Scenario(controller, Path(raw))
        QTimer.singleShot(500, scenario.start)
        QTimer.singleShot(60000, controller.shutdown)
        app.exec()

    # Leave the user's settings as they were: the scenario toggled one.
    controller.settings.set("plugins.pomodoro.enabled", False)
    controller.settings.save()

    print("\n".join(log))
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: plugin system works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
