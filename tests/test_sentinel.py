"""System sentinel decision logic.

The sampling is psutil's job and is not worth re-testing. What matters
here is when the sentinel chooses to speak, because an alert that fires on
a two-second CPU spike is worse than no alert: people turn it off, and
then it is not there for the thing that actually mattered.

Everything below drives the evaluation step directly with constructed
snapshots, so there is no dependence on what the test machine happens to
be doing.
"""

from __future__ import annotations

import time

import pytest

from core.config import DEFAULTS, Settings, _deep_merge
from core.model import Priority
from workers.sentinel import SystemSentinel, SystemSnapshot


@pytest.fixture
def settings(tmp_path):
    return Settings(_deep_merge(DEFAULTS, {}), tmp_path / "settings.json")


@pytest.fixture
def sentinel(arbiter, settings, qapp):
    return SystemSentinel(arbiter, settings)


def snap(**kwargs) -> SystemSnapshot:
    base = {
        "cpu_percent": 5.0,
        "memory_percent": 40.0,
        "top_process": "",
        "top_process_percent": 0.0,
        "top_process_pid": 0,
        "network_up": True,
        "network_name": "Wi-Fi",
        "low_disks": [],
    }
    base.update(kwargs)
    return SystemSnapshot(**base)


def feed(sentinel, snapshot, seconds: float, step: float = 3.0) -> None:
    """Replay a condition holding for a given number of seconds."""
    start = time.time()
    elapsed = 0.0
    while elapsed <= seconds:
        sentinel._evaluate(snapshot, start + elapsed)
        elapsed += step


def keys(arbiter) -> list[str]:
    return [c.key for c in arbiter.candidates()]


# ---- staying quiet -------------------------------------------------------


def test_a_healthy_machine_says_nothing(sentinel, arbiter):
    feed(sentinel, snap(), seconds=60)
    assert arbiter.candidates() == []


def test_a_brief_cpu_spike_is_ignored(sentinel, arbiter):
    # A compiler pinning the CPU for a few seconds is normal.
    busy = snap(cpu_percent=99.0, top_process="cl.exe", top_process_percent=95.0)
    feed(sentinel, busy, seconds=6)
    assert "alert.cpu" not in keys(arbiter)


def test_a_brief_network_blip_is_ignored(sentinel, arbiter):
    # A Wi-Fi roam between access points must not announce an outage.
    feed(sentinel, snap(network_up=False), seconds=3)
    assert "alert.network" not in keys(arbiter)


# ---- cpu -----------------------------------------------------------------


def test_sustained_cpu_names_the_process(sentinel, arbiter):
    busy = snap(
        cpu_percent=97.0,
        top_process="node.exe",
        top_process_percent=88.0,
        top_process_pid=4242,
    )
    feed(sentinel, busy, seconds=15)
    content = arbiter.current()
    assert content.key == "alert.cpu"
    assert content.priority == Priority.ALERT
    assert "node.exe" in content.title
    assert "88%" in content.title


def test_cpu_alert_clears_when_the_machine_calms_down(sentinel, arbiter):
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=15)
    assert "alert.cpu" in keys(arbiter)
    feed(sentinel, snap(), seconds=6)
    assert "alert.cpu" not in keys(arbiter)


def test_end_task_requires_confirmation(sentinel, arbiter):
    busy = snap(
        cpu_percent=97.0,
        top_process="node.exe",
        top_process_percent=88.0,
        top_process_pid=4242,
    )
    feed(sentinel, busy, seconds=15)
    end = next(a for a in arbiter.current().actions if a.label == "End task")
    # The island sits under the cursor's travel path, and ending the wrong
    # process loses someone's work.
    assert end.destructive is True


def test_end_task_can_be_removed_entirely(sentinel, arbiter, settings):
    settings.set("sentinel.offer_end_task", False)
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=15)
    labels = {a.label for a in arbiter.current().actions}
    assert "End task" not in labels


def test_no_cpu_alert_without_a_named_culprit(sentinel, arbiter):
    # Reporting "something is busy" with no name is not actionable.
    feed(sentinel, snap(cpu_percent=99.0), seconds=20)
    assert "alert.cpu" not in keys(arbiter)


def test_the_alert_is_raised_once_not_every_tick(sentinel, arbiter):
    raised = []
    sentinel.alert_raised.connect(lambda kind, msg: raised.append(kind))
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=60)
    assert raised.count("cpu") == 1


# ---- memory and disk -----------------------------------------------------


def test_sustained_memory_pressure_warns(sentinel, arbiter):
    feed(sentinel, snap(memory_percent=96.0), seconds=20)
    content = next(c for c in arbiter.candidates() if c.key == "alert.memory")
    assert "96%" in content.title


def test_memory_below_the_threshold_is_quiet(sentinel, arbiter):
    feed(sentinel, snap(memory_percent=80.0), seconds=60)
    assert "alert.memory" not in keys(arbiter)


def test_low_disk_warns_and_names_the_drive(sentinel, arbiter):
    low = snap(low_disks=[("C:\\", 3.0, 2_000_000_000)])
    feed(sentinel, low, seconds=10)
    content = next(c for c in arbiter.candidates() if c.key == "alert.disk")
    assert "C:\\" in content.title
    assert "1.9 GB" in content.detail


def test_several_low_disks_are_summarised(sentinel, arbiter):
    low = snap(
        low_disks=[("C:\\", 3.0, 1_000_000_000), ("D:\\", 2.0, 500_000_000)]
    )
    feed(sentinel, low, seconds=10)
    content = next(c for c in arbiter.candidates() if c.key == "alert.disk")
    assert "1 more" in content.detail


# ---- network -------------------------------------------------------------


def test_sustained_disconnection_warns(sentinel, arbiter):
    feed(sentinel, snap(network_up=False), seconds=10)
    content = next(c for c in arbiter.candidates() if c.key == "alert.network")
    assert content.title == "Network disconnected"
    assert content.progress == -1.0


def test_reconnecting_replaces_the_warning(sentinel, arbiter):
    feed(sentinel, snap(network_up=False), seconds=10)
    sentinel._evaluate(snap(network_up=True, network_name="Wi-Fi"), time.time())
    content = next(c for c in arbiter.candidates() if c.key == "alert.network")
    assert content.title == "Back online"
    assert content.expires_at is not None


def test_no_back_online_message_without_an_outage_message(sentinel, arbiter):
    # A two second blip that was never announced needs no reassurance.
    now = time.time()
    sentinel._evaluate(snap(network_up=False), now)
    sentinel._evaluate(snap(network_up=True), now + 1)
    assert "alert.network" not in keys(arbiter)


# ---- muting --------------------------------------------------------------


def test_mute_clears_and_suppresses(sentinel, arbiter):
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=15)
    sentinel._mute()
    assert arbiter.candidates() == []
    feed(sentinel, busy, seconds=30)
    assert "alert.cpu" not in keys(arbiter)


def test_mute_expires(sentinel, arbiter, settings):
    settings.set("sentinel.mute_minutes", 0.0001)
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=15)
    sentinel._mute()
    time.sleep(0.02)
    sentinel._cpu.announced = False
    feed(sentinel, busy, seconds=15)
    assert "alert.cpu" in keys(arbiter)


# ---- lifecycle -----------------------------------------------------------


def test_stop_clears_everything(sentinel, arbiter):
    busy = snap(cpu_percent=97.0, top_process="node.exe", top_process_percent=88.0)
    feed(sentinel, busy, seconds=15)
    sentinel.stop()
    assert arbiter.candidates() == []
    assert sentinel.running is False


def test_thresholds_are_configurable(sentinel, arbiter, settings):
    settings.set("sentinel.cpu_percent", 50)
    settings.set("sentinel.cpu_sustain_seconds", 3)
    moderate = snap(
        cpu_percent=60.0, top_process="python.exe", top_process_percent=55.0
    )
    feed(sentinel, moderate, seconds=6)
    assert "alert.cpu" in keys(arbiter)


def test_disk_check_can_be_disabled(sentinel, settings):
    settings.set("sentinel.disk_free_percent", 0)
    # The zero must survive, not be replaced by the default.
    assert sentinel._low_disks() == []


def test_real_sampling_does_not_raise(sentinel):
    # One pass over the real machine, to catch a psutil API change.
    sentinel.start()
    sentinel._tick()
    sentinel._tick()
    snapshot = sentinel.snapshot()
    assert 0.0 <= snapshot.cpu_percent <= 100.0
    assert 0.0 <= snapshot.memory_percent <= 100.0
    sentinel.stop()
