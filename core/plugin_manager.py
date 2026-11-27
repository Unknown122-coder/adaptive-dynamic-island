"""Discovering, loading and containing plugins.

Discovery is a directory scan, not a package import, because the whole
point is that a user can drop one file into a folder and restart. User
plugins shadow shipped ones of the same name, so a bundled sample can be
replaced without editing anything in the install directory.

The hard requirement is containment. Plugin code is written by someone
other than the island's author, it runs on the GUI thread, and a single
unhandled exception in a timer callback would otherwise take the whole
overlay down. Every entry point into plugin code therefore goes through
:meth:`PluginManager._guard`, which records the failure, and a plugin that
fails repeatedly is disabled for the session rather than allowed to spam.

What this is not is a sandbox. A plugin is ordinary Python with the user's
privileges; see the note in :mod:`core.plugin_api`.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core import paths
from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.plugin_api import IslandPlugin, PluginContext, validate

# A plugin that raises this many times is switched off for the session.
FAILURE_LIMIT = 3


@dataclass
class LoadedPlugin:
    """A discovered plugin and everything the host knows about it."""

    plugin: IslandPlugin
    source: Path
    context: PluginContext | None = None
    active: bool = False
    failures: int = 0
    disabled_reason: str | None = None

    @property
    def id(self) -> str:
        return self.plugin.id

    @property
    def name(self) -> str:
        return self.plugin.name or self.plugin.id

    @property
    def description(self) -> str:
        return self.plugin.description

    @property
    def healthy(self) -> bool:
        return self.disabled_reason is None


@dataclass
class DiscoveryReport:
    """What a scan found, including what it refused and why."""

    loaded: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{len(self.loaded)} loaded"]
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        return ", ".join(parts)


class PluginManager(QObject):
    """Owns the plugin lifecycle."""

    plugins_changed = Signal()
    plugin_failed = Signal(str, str)  # plugin id, message

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._arbiter = arbiter
        self._settings = settings
        self._plugins: dict[str, LoadedPlugin] = {}
        self._report = DiscoveryReport()

    # ---- discovery -----------------------------------------------------

    def discover(self, directories: list[Path] | None = None) -> DiscoveryReport:
        """Scan for plugins. Safe to call again; reloads everything."""
        self.unload_all()
        self._plugins.clear()
        report = DiscoveryReport()

        for directory in directories or paths.plugin_dirs():
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.py")):
                # Leading underscore marks a helper module, not a plugin.
                if file.name.startswith("_"):
                    continue
                self._load_file(file, report)

        self._report = report
        self.plugins_changed.emit()
        return report

    def _load_file(self, file: Path, report: DiscoveryReport) -> None:
        classes = self._import_plugin_classes(file, report)
        for cls in classes:
            try:
                instance = cls()
            except Exception as exc:
                report.rejected.append((file.name, f"could not construct: {exc}"))
                continue

            problem = validate(instance)
            if problem is not None:
                report.rejected.append((file.name, problem))
                continue

            if instance.id in self._plugins:
                # A user plugin shadowing a shipped one is intentional, and
                # directories are scanned user-first.
                report.rejected.append(
                    (file.name, f"id {instance.id!r} already provided by "
                     f"{self._plugins[instance.id].source.name}")
                )
                continue

            self._plugins[instance.id] = LoadedPlugin(
                plugin=instance, source=file
            )
            report.loaded.append(instance.id)

    def _import_plugin_classes(
        self, file: Path, report: DiscoveryReport
    ) -> list[type[IslandPlugin]]:
        # A unique module name per path keeps two plugins with the same
        # filename in different directories from colliding in sys.modules.
        module_name = f"island_plugin_{abs(hash(str(file)))}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file)
            if spec is None or spec.loader is None:
                report.rejected.append((file.name, "not importable"))
                return []
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            # Import-time errors are the most common plugin failure, and
            # the traceback is what the author actually needs.
            print(f"[plugins] {file.name} failed to import: {exc}")
            traceback.print_exc()
            report.rejected.append((file.name, f"import error: {exc}"))
            sys.modules.pop(module_name, None)
            return []

        found: list[type[IslandPlugin]] = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, IslandPlugin) or obj is IslandPlugin:
                continue
            if inspect.isabstract(obj):
                continue
            # Ignore classes the module merely imported from elsewhere.
            if obj.__module__ != module_name:
                continue
            found.append(obj)
        if not found:
            report.rejected.append((file.name, "no IslandPlugin subclass"))
        return found

    # ---- lifecycle -----------------------------------------------------

    def start_enabled(self) -> None:
        """Activate every plugin the settings say should be on."""
        for plugin_id, entry in self._plugins.items():
            if self.is_enabled(plugin_id, entry.plugin.enabled_by_default):
                self.activate(plugin_id)

    def is_enabled(self, plugin_id: str, default: bool = True) -> bool:
        return bool(self._settings.get(f"plugins.{plugin_id}.enabled", default))

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        self._settings.set(f"plugins.{plugin_id}.enabled", enabled)
        self._settings.save()
        if enabled:
            self.activate(plugin_id)
        else:
            self.deactivate(plugin_id)

    def activate(self, plugin_id: str) -> bool:
        entry = self._plugins.get(plugin_id)
        if entry is None or entry.active:
            return False
        if not entry.healthy:
            return False

        handle = ProviderHandle(self._arbiter, f"plugin.{plugin_id}")
        context = PluginContext(
            plugin_id=plugin_id,
            handle=handle,
            settings=dict(self._settings.get(f"plugins.{plugin_id}", {}) or {}),
            owner=self,
            logger=lambda message, pid=plugin_id: self._on_log(pid, message),
        )
        entry.context = context
        ok = self._guard(entry, "setup", lambda: entry.plugin.setup(context))
        entry.active = ok
        if not ok:
            context._stop_timers()  # noqa: SLF001 - host owns the context
            handle.clear()
            entry.context = None
        self.plugins_changed.emit()
        return ok

    def deactivate(self, plugin_id: str) -> None:
        entry = self._plugins.get(plugin_id)
        if entry is None or not entry.active:
            return
        self._guard(entry, "teardown", entry.plugin.teardown)
        if entry.context is not None:
            entry.context._stop_timers()  # noqa: SLF001 - host owns it
            entry.context.hide_all()
            entry.context = None
        entry.active = False
        self.plugins_changed.emit()

    def unload_all(self) -> None:
        for plugin_id in list(self._plugins):
            self.deactivate(plugin_id)

    def reload(self) -> DiscoveryReport:
        """Re-scan from disk, preserving which plugins were enabled."""
        report = self.discover()
        self.start_enabled()
        return report

    # ---- containment ---------------------------------------------------

    def _guard(self, entry: LoadedPlugin, what: str, call) -> bool:
        """Run plugin code, absorbing anything it throws."""
        try:
            call()
            return True
        except Exception as exc:
            entry.failures += 1
            message = f"{what}() raised {type(exc).__name__}: {exc}"
            print(f"[plugins] {entry.id}: {message}")
            traceback.print_exc()
            if entry.failures >= FAILURE_LIMIT:
                entry.disabled_reason = (
                    f"disabled after {entry.failures} failures"
                )
                entry.active = False
            self.plugin_failed.emit(entry.id, message)
            return False

    def _on_log(self, plugin_id: str, message: str) -> None:
        print(f"[plugin:{plugin_id}] {message}")

    # ---- introspection --------------------------------------------------

    def all(self) -> list[LoadedPlugin]:
        return sorted(self._plugins.values(), key=lambda e: e.name.lower())

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def active_ids(self) -> list[str]:
        return sorted(e.id for e in self._plugins.values() if e.active)

    def report(self) -> DiscoveryReport:
        return self._report

    def describe(self) -> str:
        """Lines for the diagnostics panel."""
        if not self._plugins:
            return "  none discovered"
        lines = []
        for entry in self.all():
            state = "active" if entry.active else "idle"
            if not entry.healthy:
                state = entry.disabled_reason or "disabled"
            lines.append(
                f"  {entry.id} ({entry.plugin.version}) [{state}] "
                f"from {entry.source.name}"
            )
        for name, reason in self._report.rejected:
            lines.append(f"  {name} REJECTED: {reason}")
        return "\n".join(lines)
