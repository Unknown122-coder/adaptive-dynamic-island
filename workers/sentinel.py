"""System and network alerts.

Task Manager already lists processes and Windows already shows a network
icon, so repeating either would be pointless. What neither does is tell
you *without being asked*. The value here is proactive and specific: your
fan is loud because of this executable, your network went away, this disk
is nearly full.

Three rules shape it.

**Quiet by default.** Nothing is published while the machine is healthy.
A permanent CPU readout is a widget, not an alert, and the taskbar
already has one.

**Sustained, not instantaneous.** Every condition has to persist for
several seconds before it says anything. A compiler pinning the CPU for
two seconds is normal; the same thing for fifteen is worth knowing.

**Cheap when idle, and never on the GUI thread.** The expensive part,
enumerating every process to find the culprit, runs only after a single
cheap global reading says something is wrong. Measured on a real machine,
that scan takes around 350 ms, which on the GUI thread would freeze the
island's animation for a third of a second every time the CPU spiked, so
all sampling happens on a worker and only the finished snapshot crosses
back. Even the healthy-path sample costs about 9 ms, which is not
something to spend on the thread that draws.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psutil
from PySide6.QtCore import QObject, QThread, Signal

from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.model import Action, IslandContent, Priority, Urgency

# Processes that are supposed to use the CPU, or that naming would only
# confuse the user about. Blaming the kernel for being busy is not a
# useful alert.
IGNORED_PROCESSES = frozenset(
    {
        "system idle process",
        "system",
        "memory compression",
        "registry",
        "idle",
    }
)

# Addresses in this range mean "DHCP failed", which is a disconnection
# wearing a connected-looking hat.
APIPA_PREFIX = "169.254."


@dataclass
class _Condition:
    """Tracks how long something has been true."""

    since: float | None = None
    announced: bool = False

    def update(self, active: bool, now: float) -> None:
        if active:
            if self.since is None:
                self.since = now
        else:
            self.since = None
            self.announced = False

    def held_for(self, now: float) -> float:
        return 0.0 if self.since is None else now - self.since

    def ready(self, now: float, seconds: float) -> bool:
        return (
            self.since is not None
            and not self.announced
            and self.held_for(now) >= seconds
        )


@dataclass
class SystemSnapshot:
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    top_process: str = ""
    top_process_percent: float = 0.0
    top_process_pid: int = 0
    network_up: bool = True
    network_name: str = ""
    low_disks: list[tuple[str, float, int]] = field(default_factory=list)


class _SamplerThread(QThread):
    """Reads the machine's state away from the GUI thread."""

    sampled = Signal(object)  # SystemSnapshot

    def __init__(
        self,
        sample: Callable[[], SystemSnapshot | None],
        interval: float,
    ) -> None:
        super().__init__()
        self._sample = sample
        self._interval = max(1.0, interval)
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # The first reading of cpu_percent is always 0.0 because it has no
        # previous sample to compare against.
        try:
            psutil.cpu_percent(interval=None)
        except (psutil.Error, OSError):
            return
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            snapshot = self._sample()
            if snapshot is not None:
                self.sampled.emit(snapshot)


class SystemSentinel(QObject):
    """Watches the machine and speaks only when something is wrong."""

    alert_raised = Signal(str, str)  # kind, human message

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "alert")
        self._settings = settings
        self._snapshot = SystemSnapshot()
        self._running = False

        self._cpu = _Condition()
        self._memory = _Condition()
        self._network = _Condition()
        self._disk = _Condition()

        self._process_cache: dict[int, psutil.Process] = {}
        self._primed = False
        self._cores = max(1, psutil.cpu_count() or 1)
        self._was_connected = True
        self._muted_until = 0.0
        self._partitions: list[str] | None = None
        self._disk_countdown = 0
        self._cached_low_disks: list[tuple[str, float, int]] = []

        self._thread: _SamplerThread | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        interval = self._settings.number("sentinel.sample_interval_seconds", 3)
        self._thread = _SamplerThread(self._sample, interval)
        self._thread.sampled.connect(self._on_sample)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.request_stop()
            self._thread.wait(4000)
            self._thread = None
        self._process_cache.clear()
        self._primed = False
        self._partitions = None
        self._handle.clear()
        for condition in (self._cpu, self._memory, self._network, self._disk):
            condition.since = None
            condition.announced = False

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> SystemSnapshot:
        return self._snapshot

    # ---- sampling (worker thread) ----------------------------------------

    def _sample(self) -> SystemSnapshot | None:
        """Read everything. Runs on the sampler thread, never the GUI one."""
        snapshot = SystemSnapshot()
        try:
            snapshot.cpu_percent = float(psutil.cpu_percent(interval=None))
            snapshot.memory_percent = float(psutil.virtual_memory().percent)
        except (psutil.Error, OSError):
            return None

        cpu_threshold = self._settings.number("sentinel.cpu_percent", 85)
        if snapshot.cpu_percent >= cpu_threshold:
            # Only now is it worth walking the process table, which costs
            # roughly 350 ms.
            self._fill_top_process(snapshot)
        else:
            self._process_cache.clear()
            self._primed = False

        snapshot.network_up, snapshot.network_name = self._network_state()

        # Disk usage barely moves between samples, and enumerating
        # partitions is the most expensive part of the healthy path.
        if self._disk_countdown <= 0:
            snapshot.low_disks = self._low_disks()
            self._cached_low_disks = snapshot.low_disks
            self._disk_countdown = 10
        else:
            self._disk_countdown -= 1
            snapshot.low_disks = self._cached_low_disks
        return snapshot

    def _on_sample(self, snapshot: SystemSnapshot) -> None:
        """Back on the GUI thread, with a finished reading."""
        self._snapshot = snapshot
        self._evaluate(snapshot, time.time())

    def _tick(self) -> None:
        """Sample and evaluate once, synchronously. For tests and probes."""
        snapshot = self._sample()
        if snapshot is not None:
            self._on_sample(snapshot)

    def _fill_top_process(self, snapshot: SystemSnapshot) -> None:
        """Find the busiest process, normalised to whole-machine percent."""
        best_name, best_percent, best_pid = "", 0.0, 0
        live: dict[int, psutil.Process] = {}
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                pid = proc.info["pid"]
                cached = self._process_cache.get(pid)
                handle = cached if cached is not None else proc
                live[pid] = handle
                try:
                    # Relative to one core, so divide to match the way
                    # Task Manager reports it.
                    percent = handle.cpu_percent(None) / self._cores
                except (psutil.Error, OSError):
                    continue
                name = (proc.info.get("name") or "").strip()
                if not name or name.lower() in IGNORED_PROCESSES:
                    continue
                if percent > best_percent:
                    best_name, best_percent, best_pid = name, percent, pid
        except (psutil.Error, OSError):
            return

        self._process_cache = live
        if not self._primed:
            # The first pass only primes the per-process counters; their
            # readings are meaningless until there is a previous sample.
            self._primed = True
            return
        snapshot.top_process = best_name
        snapshot.top_process_percent = best_percent
        snapshot.top_process_pid = best_pid

    @staticmethod
    def _network_state() -> tuple[bool, str]:
        """Link state from local interface data. Sends no traffic.

        An interface counts as connected when it is up and holds a real
        IPv4 address. A 169.254 address means DHCP failed, which looks
        connected to a naive check and is not.
        """
        try:
            stats = psutil.net_if_stats()
            addresses = psutil.net_if_addrs()
        except (psutil.Error, OSError):
            return True, ""

        import socket

        for name, info in stats.items():
            if not info.isup:
                continue
            lowered = name.lower()
            if "loopback" in lowered or lowered.startswith("lo"):
                continue
            for address in addresses.get(name, []):
                if address.family != socket.AF_INET:
                    continue
                ip = address.address or ""
                if ip.startswith(("127.", APIPA_PREFIX)) or not ip:
                    continue
                return True, name
        return False, ""

    def _low_disks(self) -> list[tuple[str, float, int]]:
        """Fixed drives below the free-space threshold."""
        threshold = self._settings.number("sentinel.disk_free_percent", 5)
        if threshold <= 0:
            return []
        out: list[tuple[str, float, int]] = []
        if self._partitions is None:
            try:
                self._partitions = [
                    p.mountpoint
                    for p in psutil.disk_partitions(all=False)
                    # Skip removable and optical media: a full USB stick is
                    # not an emergency and an empty card reader is not a
                    # disk at all.
                    if "cdrom" not in p.opts and p.fstype
                ]
            except (psutil.Error, OSError):
                self._partitions = []
        for mountpoint in self._partitions:
            try:
                usage = psutil.disk_usage(mountpoint)
            except (psutil.Error, OSError, PermissionError):
                continue
            free_percent = 100.0 - usage.percent
            if free_percent <= threshold:
                out.append((mountpoint, free_percent, usage.free))
        return out

    # ---- decisions -------------------------------------------------------

    def _evaluate(self, snapshot: SystemSnapshot, now: float) -> None:
        muted = now < self._muted_until

        cpu_threshold = self._settings.number("sentinel.cpu_percent", 85)
        sustain = self._settings.number("sentinel.cpu_sustain_seconds", 10)
        self._cpu.update(snapshot.cpu_percent >= cpu_threshold, now)
        if self._cpu.ready(now, sustain) and snapshot.top_process and not muted:
            self._cpu.announced = True
            self._announce_cpu(snapshot, now)
        elif self._cpu.since is None:
            self._handle.retract("cpu")

        mem_threshold = self._settings.number("sentinel.memory_percent", 92)
        mem_sustain = self._settings.number("sentinel.memory_sustain_seconds", 15)
        self._memory.update(snapshot.memory_percent >= mem_threshold, now)
        if self._memory.ready(now, mem_sustain) and not muted:
            self._memory.announced = True
            self._announce_memory(snapshot)
        elif self._memory.since is None:
            self._handle.retract("memory")

        net_sustain = self._settings.number("sentinel.network_sustain_seconds", 6)
        # Read this before update(), which clears the flag as soon as the
        # condition stops holding. Without the capture, a reconnection can
        # never tell whether an outage was ever announced, and the "back
        # online" message is silently skipped.
        announced_outage = self._network.announced
        self._network.update(not snapshot.network_up, now)
        if self._network.ready(now, net_sustain) and not muted:
            self._network.announced = True
            self._announce_network_down()
        elif snapshot.network_up and not self._was_connected:
            self._announce_network_back(snapshot, announced_outage)
        self._was_connected = snapshot.network_up

        self._disk.update(bool(snapshot.low_disks), now)
        if self._disk.ready(now, 5) and not muted:
            self._disk.announced = True
            self._announce_disk(snapshot)
        elif self._disk.since is None:
            self._handle.retract("disk")

    # ---- announcements ---------------------------------------------------

    def _announce_cpu(self, snapshot: SystemSnapshot, now: float) -> None:
        held = int(self._cpu.held_for(now))
        actions = [Action("Mute 10 min", self._mute)]
        if self._settings.get("sentinel.offer_end_task", True):
            actions.append(
                Action(
                    "End task",
                    lambda pid=snapshot.top_process_pid: self._end_task(pid),
                    # Confirm-on-second-click. The island sits under the
                    # cursor's travel path and ending the wrong process
                    # loses work.
                    destructive=True,
                )
            )
        self._handle.publish_content(
            IslandContent(
                key="alert.cpu",
                priority=Priority.ALERT,
                title=(
                    f"{snapshot.top_process} is using "
                    f"{snapshot.top_process_percent:.0f}% CPU"
                ),
                detail=f"sustained for {held}s · system at "
                f"{snapshot.cpu_percent:.0f}%",
                glyph="cpu",
                urgency=Urgency.WARNING,
                actions=tuple(actions),
                auto_expand=True,
                collapsed=f"{snapshot.top_process} busy",
            )
        )
        self.alert_raised.emit(
            "cpu",
            f"{snapshot.top_process} is using "
            f"{snapshot.top_process_percent:.0f}% CPU",
        )

    def _announce_memory(self, snapshot: SystemSnapshot) -> None:
        self._handle.publish_content(
            IslandContent(
                key="alert.memory",
                priority=Priority.ALERT,
                title=f"Memory at {snapshot.memory_percent:.0f}%",
                detail="the machine will start swapping",
                glyph="pulse",
                urgency=Urgency.WARNING,
                actions=(Action("Mute 10 min", self._mute),),
                collapsed=f"memory {snapshot.memory_percent:.0f}%",
            )
        )
        self.alert_raised.emit("memory", f"Memory at {snapshot.memory_percent:.0f}%")

    def _announce_network_down(self) -> None:
        self._handle.publish_content(
            IslandContent(
                key="alert.network",
                priority=Priority.ALERT,
                title="Network disconnected",
                detail="no interface has a working address",
                glyph="wifi-off",
                urgency=Urgency.WARNING,
                progress=-1.0,
                auto_expand=True,
                collapsed="offline",
            )
        )
        self.alert_raised.emit("network", "Network disconnected")

    def _announce_network_back(
        self, snapshot: SystemSnapshot, had_announced: bool
    ) -> None:
        self._handle.retract("network")
        if not had_announced:
            # It never got as far as reporting the outage, so there is
            # nothing to reassure anyone about.
            return
        self._network.announced = False
        self._handle.publish_content(
            IslandContent(
                key="alert.network",
                priority=Priority.ALERT,
                title="Back online",
                detail=snapshot.network_name or "connection restored",
                glyph="pulse",
                urgency=Urgency.SUCCESS,
                collapsed="back online",
                expires_at=ProviderHandle.ttl(5),
            )
        )

    def _announce_disk(self, snapshot: SystemSnapshot) -> None:
        from workers.media import human_bytes

        drive, percent, free = snapshot.low_disks[0]
        extra = (
            f" and {len(snapshot.low_disks) - 1} more"
            if len(snapshot.low_disks) > 1
            else ""
        )
        self._handle.publish_content(
            IslandContent(
                key="alert.disk",
                priority=Priority.ALERT,
                title=f"{drive} is nearly full",
                detail=f"{human_bytes(free)} free, {percent:.0f}%{extra}",
                glyph="warning",
                urgency=Urgency.WARNING,
                actions=(Action("Mute 10 min", self._mute),),
                collapsed=f"{drive} full",
            )
        )
        self.alert_raised.emit("disk", f"{drive} is nearly full")

    # ---- actions ---------------------------------------------------------

    def _mute(self) -> None:
        """Silence alerts for a while.

        A machine that is genuinely busy will re-trigger immediately, and
        an alert the user has already seen and accepted should not keep
        interrupting them.
        """
        minutes = self._settings.number("sentinel.mute_minutes", 10)
        self._muted_until = time.time() + minutes * 60
        self._handle.clear()

    @staticmethod
    def _end_task(pid: int) -> bool:
        """Terminate a process. Only reachable via a confirmed click."""
        if not pid:
            return False
        try:
            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=3)
            except psutil.TimeoutExpired:
                process.kill()
            return True
        except (psutil.Error, OSError) as exc:
            print(f"[sentinel] could not end pid {pid}: {exc}")
            return False

    def describe(self) -> str:
        snapshot = self._snapshot
        lines = [
            f"  running: {self._running}",
            f"  cpu: {snapshot.cpu_percent:.0f}%  memory: "
            f"{snapshot.memory_percent:.0f}%",
            f"  network: {'up' if snapshot.network_up else 'DOWN'} "
            f"{snapshot.network_name}",
        ]
        if snapshot.top_process:
            lines.append(
                f"  busiest: {snapshot.top_process} "
                f"{snapshot.top_process_percent:.0f}%"
            )
        if snapshot.low_disks:
            lines.append(
                "  low disks: "
                + ", ".join(f"{d} {p:.0f}%" for d, p, _ in snapshot.low_disks)
            )
        if time.time() < self._muted_until:
            remaining = int(self._muted_until - time.time())
            lines.append(f"  muted for another {remaining}s")
        return "\n".join(lines)
