"""Plugin discovery, lifecycle and containment.

Plugin code is written by someone other than the island's author and runs
on the GUI thread, so the tests that matter are the ones about what a bad
plugin cannot do: it cannot crash the island, it cannot outrank a privacy
warning, it cannot leak timers past its own lifetime, and it cannot claim
another provider's namespace.
"""

from __future__ import annotations

import textwrap

import pytest

from core.config import DEFAULTS, Settings, _deep_merge
from core.model import Priority
from core.plugin_manager import FAILURE_LIMIT, PluginManager


@pytest.fixture
def settings(tmp_path):
    return Settings(_deep_merge(DEFAULTS, {}), tmp_path / "settings.json")


@pytest.fixture
def plugin_dir(tmp_path):
    directory = tmp_path / "plugins"
    directory.mkdir()
    return directory


@pytest.fixture
def manager(arbiter, settings, qapp):
    instance = PluginManager(arbiter, settings)
    yield instance
    instance.unload_all()


def write(directory, name: str, body: str):
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


GOOD = """
    from core.plugin_api import IslandPlugin

    class Demo(IslandPlugin):
        id = "demo"
        name = "Demo plugin"
        description = "does a thing"

        def setup(self, ctx):
            ctx.show("hello", detail="from the demo")
"""


# ---- discovery -----------------------------------------------------------


def test_discovers_a_valid_plugin(manager, plugin_dir):
    write(plugin_dir, "demo.py", GOOD)
    report = manager.discover([plugin_dir])
    assert report.loaded == ["demo"]
    assert manager.get("demo") is not None


def test_ignores_underscore_prefixed_files(manager, plugin_dir):
    write(plugin_dir, "_helpers.py", GOOD)
    assert manager.discover([plugin_dir]).loaded == []


def test_rejects_a_file_with_no_plugin_class(manager, plugin_dir):
    write(plugin_dir, "nothing.py", "VALUE = 1\n")
    report = manager.discover([plugin_dir])
    assert report.loaded == []
    assert any("no IslandPlugin" in reason for _, reason in report.rejected)


def test_survives_a_plugin_that_fails_to_import(manager, plugin_dir, capsys):
    write(plugin_dir, "broken.py", "import a_module_that_does_not_exist\n")
    write(plugin_dir, "demo.py", GOOD)
    report = manager.discover([plugin_dir])
    # The good plugin must still load.
    assert report.loaded == ["demo"]
    assert any("import error" in reason for _, reason in report.rejected)


def test_rejects_a_plugin_without_an_id(manager, plugin_dir):
    write(
        plugin_dir,
        "anon.py",
        """
        from core.plugin_api import IslandPlugin

        class Anon(IslandPlugin):
            name = "no id"
            def setup(self, ctx): pass
        """,
    )
    report = manager.discover([plugin_dir])
    assert report.loaded == []
    assert any("missing an id" in reason for _, reason in report.rejected)


def test_rejects_an_id_containing_a_dot(manager, plugin_dir):
    # A dot would let a plugin address another provider's content keys.
    write(
        plugin_dir,
        "sneaky.py",
        """
        from core.plugin_api import IslandPlugin

        class Sneaky(IslandPlugin):
            id = "privacy.faces"
            name = "Sneaky"
            def setup(self, ctx): pass
        """,
    )
    report = manager.discover([plugin_dir])
    assert report.loaded == []
    assert any("must not contain a dot" in reason for _, reason in report.rejected)


def test_rejects_a_duplicate_id(manager, plugin_dir):
    write(plugin_dir, "one.py", GOOD)
    write(plugin_dir, "two.py", GOOD)
    report = manager.discover([plugin_dir])
    assert report.loaded == ["demo"]
    assert any("already provided" in reason for _, reason in report.rejected)


# ---- lifecycle -----------------------------------------------------------


def test_activate_publishes_into_a_namespace(manager, plugin_dir, arbiter):
    write(plugin_dir, "demo.py", GOOD)
    manager.discover([plugin_dir])
    assert manager.activate("demo")
    current = arbiter.current()
    assert current is not None
    assert current.key == "plugin.demo.main"
    assert current.title == "hello"


def test_deactivate_removes_its_content(manager, plugin_dir, arbiter):
    write(plugin_dir, "demo.py", GOOD)
    manager.discover([plugin_dir])
    manager.activate("demo")
    manager.deactivate("demo")
    assert arbiter.current() is None
    assert manager.active_ids() == []


def test_enabled_by_default_is_respected(manager, plugin_dir):
    write(
        plugin_dir,
        "quiet.py",
        """
        from core.plugin_api import IslandPlugin

        class Quiet(IslandPlugin):
            id = "quiet"
            name = "Quiet"
            enabled_by_default = False
            def setup(self, ctx): ctx.show("here")
        """,
    )
    manager.discover([plugin_dir])
    manager.start_enabled()
    assert manager.active_ids() == []


def test_set_enabled_persists_and_activates(manager, plugin_dir, settings):
    write(plugin_dir, "demo.py", GOOD)
    manager.discover([plugin_dir])
    manager.set_enabled("demo", True)
    assert manager.active_ids() == ["demo"]
    assert settings.get("plugins.demo.enabled") is True


def test_reload_rediscovers_from_disk(manager, plugin_dir):
    write(plugin_dir, "demo.py", GOOD)
    manager.discover([plugin_dir])
    assert manager.get("second") is None
    write(plugin_dir, "second.py", GOOD.replace('"demo"', '"second"'))
    manager.discover([plugin_dir])
    assert manager.get("second") is not None


# ---- containment ---------------------------------------------------------


def test_a_plugin_cannot_outrank_an_alert(manager, plugin_dir, arbiter):
    # Even if a plugin asks for the highest urgency, its priority is
    # clamped, so anything protecting the user still wins.
    write(
        plugin_dir,
        "loud.py",
        """
        from core.plugin_api import IslandPlugin

        class Loud(IslandPlugin):
            id = "loud"
            name = "Loud"
            def setup(self, ctx):
                ctx.show("LOOK AT ME", urgency="critical")
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("loud")
    published = arbiter.current()
    assert published.priority == Priority.PLUGIN
    assert published.priority < Priority.ALERT
    assert published.priority < Priority.PRIVACY


def test_a_plugin_that_raises_in_setup_does_not_activate(manager, plugin_dir):
    write(
        plugin_dir,
        "bad.py",
        """
        from core.plugin_api import IslandPlugin

        class Bad(IslandPlugin):
            id = "bad"
            name = "Bad"
            def setup(self, ctx):
                raise RuntimeError("boom")
        """,
    )
    manager.discover([plugin_dir])
    assert manager.activate("bad") is False
    assert manager.active_ids() == []


def test_a_raising_plugin_leaves_no_content_behind(manager, plugin_dir, arbiter):
    write(
        plugin_dir,
        "halfbad.py",
        """
        from core.plugin_api import IslandPlugin

        class HalfBad(IslandPlugin):
            id = "halfbad"
            name = "Half bad"
            def setup(self, ctx):
                ctx.show("published before failing")
                raise ValueError("then it broke")
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("halfbad")
    assert arbiter.current() is None


def test_repeated_failures_disable_the_plugin(manager, plugin_dir):
    write(
        plugin_dir,
        "bad.py",
        """
        from core.plugin_api import IslandPlugin

        class Bad(IslandPlugin):
            id = "bad"
            name = "Bad"
            def setup(self, ctx):
                raise RuntimeError("boom")
        """,
    )
    manager.discover([plugin_dir])
    for _ in range(FAILURE_LIMIT):
        manager.activate("bad")
    entry = manager.get("bad")
    assert not entry.healthy
    assert "disabled" in entry.disabled_reason
    # Further attempts are refused outright rather than re-running it.
    assert manager.activate("bad") is False


def test_a_failing_plugin_emits_a_signal(manager, plugin_dir, qapp):
    write(
        plugin_dir,
        "bad.py",
        """
        from core.plugin_api import IslandPlugin

        class Bad(IslandPlugin):
            id = "bad"
            name = "Bad"
            def setup(self, ctx):
                raise RuntimeError("boom")
        """,
    )
    seen = []
    manager.plugin_failed.connect(lambda pid, msg: seen.append((pid, msg)))
    manager.discover([plugin_dir])
    manager.activate("bad")
    assert seen and seen[0][0] == "bad"
    assert "RuntimeError" in seen[0][1]


def test_timers_are_stopped_on_deactivate(manager, plugin_dir):
    write(
        plugin_dir,
        "ticker.py",
        """
        from core.plugin_api import IslandPlugin

        class Ticker(IslandPlugin):
            id = "ticker"
            name = "Ticker"
            def setup(self, ctx):
                self.timer = ctx.every(50, lambda: ctx.show("tick"))
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("ticker")
    entry = manager.get("ticker")
    timer = entry.plugin.timer
    assert timer.isActive()
    manager.deactivate("ticker")
    # The host owns plugin timers precisely so this cannot be forgotten.
    assert not timer.isActive()


def test_a_raising_timer_callback_does_not_propagate(manager, plugin_dir, qapp):
    write(
        plugin_dir,
        "badtick.py",
        """
        from core.plugin_api import IslandPlugin

        class BadTick(IslandPlugin):
            id = "badtick"
            name = "Bad tick"
            def setup(self, ctx):
                self.timer = ctx.every(50, self.explode)
            def explode(self):
                raise RuntimeError("tick boom")
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("badtick")
    entry = manager.get("badtick")
    # Firing the callback directly must not raise out of the guard.
    entry.plugin.timer.timeout.emit()


def test_teardown_errors_do_not_prevent_unloading(manager, plugin_dir):
    write(
        plugin_dir,
        "rude.py",
        """
        from core.plugin_api import IslandPlugin

        class Rude(IslandPlugin):
            id = "rude"
            name = "Rude"
            def setup(self, ctx): ctx.show("hi")
            def teardown(self): raise RuntimeError("will not go quietly")
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("rude")
    manager.deactivate("rude")
    assert manager.active_ids() == []


# ---- settings ------------------------------------------------------------


def test_a_plugin_reads_its_own_settings(manager, plugin_dir, settings, arbiter):
    settings.set("plugins.cfg.label", "configured")
    write(
        plugin_dir,
        "cfg.py",
        """
        from core.plugin_api import IslandPlugin

        class Cfg(IslandPlugin):
            id = "cfg"
            name = "Cfg"
            def setup(self, ctx):
                ctx.show(ctx.setting("label", "default"))
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("cfg")
    assert arbiter.current().title == "configured"


def test_missing_settings_fall_back_to_the_default(manager, plugin_dir, arbiter):
    write(
        plugin_dir,
        "cfg.py",
        """
        from core.plugin_api import IslandPlugin

        class Cfg(IslandPlugin):
            id = "cfg"
            name = "Cfg"
            def setup(self, ctx):
                ctx.show(ctx.setting("label", "default"))
        """,
    )
    manager.discover([plugin_dir])
    manager.activate("cfg")
    assert arbiter.current().title == "default"


# ---- the shipped samples -------------------------------------------------


def test_bundled_samples_load(manager):
    from core import paths

    report = manager.discover([paths.app_root() / "plugins"])
    assert "pomodoro" in report.loaded
    assert "battery" in report.loaded
    assert report.rejected == []


def test_bundled_samples_are_off_by_default(manager):
    from core import paths

    manager.discover([paths.app_root() / "plugins"])
    manager.start_enabled()
    # A sample that turns itself on is a sample nobody asked for.
    assert manager.active_ids() == []
