"""The privacy guard's decision logic.

Everything here runs without OpenCV, without the face model and without a
camera, by driving the guard's state machine directly. That is deliberate:
the detector is a dependency, but the decisions are where the harm lives.
A guard that opens the camera at the wrong moment, or that interrupts
someone because one frame was odd, is a worse product than one that
occasionally misses a face.

Camera hardware is checked separately by scripts/check_vision.py, which
the user runs themselves.
"""

from __future__ import annotations

import time

import pytest

from core.config import DEFAULTS, Settings, _deep_merge
from core.model import Priority
from core.osbridge.consent import CapabilityUser, ConsentWatcher
from core.osbridge.window_hooks import ForegroundInfo, ForegroundWatcher
from workers.privacy_guard import PrivacyGuard


@pytest.fixture
def settings(tmp_path):
    return Settings(_deep_merge(DEFAULTS, {}), tmp_path / "settings.json")


class FakeWorker:
    """Stands in for the vision thread; records arm and disarm calls."""

    def __init__(self) -> None:
        self.armed = False
        self.backend = "yunet"
        self.history: list[str] = []

    def arm(self) -> None:
        self.armed = True
        self.history.append("arm")

    def disarm(self) -> None:
        self.armed = False
        self.history.append("disarm")

    def request_stop(self) -> None:
        self.history.append("stop")

    def wait(self, _ms: int) -> bool:
        return True


@pytest.fixture
def guard(arbiter, settings, qapp):
    foreground = ForegroundWatcher()
    consent = ConsentWatcher()
    instance = PrivacyGuard(arbiter, settings, foreground, consent)
    # Pretend the feature started successfully, without touching OpenCV.
    instance._state.enabled = True
    instance._worker = FakeWorker()
    yield instance


def set_foreground(guard, process: str) -> None:
    guard._foreground._last = ForegroundInfo(
        hwnd=1,
        pid=1,
        process=process,
        exe_path=None,
        title="window",
        fullscreen=False,
    )


def set_webcam_users(guard, names: list[str]) -> None:
    guard._consent._latest = {
        "webcam": [
            CapabilityUser("webcam", n, n, packaged=False) for n in names
        ],
        "microphone": [],
    }


def sample(guard, faces: int, times: int = 1) -> None:
    for _ in range(times):
        guard._on_sample(faces, 0.08)


# ---- when the camera may be opened --------------------------------------


def test_does_not_arm_for_an_ordinary_window(guard):
    set_foreground(guard, "notepad.exe")
    guard._evaluate()
    assert guard._worker.armed is False


def test_arms_for_a_configured_sensitive_app(guard):
    set_foreground(guard, "keepassxc.exe")
    guard._evaluate()
    assert guard._worker.armed is True


def test_sensitive_match_is_case_insensitive(guard):
    set_foreground(guard, "KeePassXC.EXE")
    guard._evaluate()
    assert guard._worker.armed is True


def test_manual_watch_arms_regardless_of_window(guard):
    set_foreground(guard, "notepad.exe")
    guard.set_manual_watch(True)
    assert guard._worker.armed is True


def test_disabling_manual_watch_disarms(guard):
    set_foreground(guard, "notepad.exe")
    guard.set_manual_watch(True)
    guard.set_manual_watch(False)
    assert guard._worker.armed is False


def test_empty_sensitive_list_never_arms_on_its_own(guard, settings):
    settings.set("privacy.sensitive_processes", [])
    set_foreground(guard, "keepassxc.exe")
    guard._evaluate()
    assert guard._worker.armed is False
    assert "no sensitive apps" in guard.state().reason


# ---- yielding the camera -------------------------------------------------


def test_does_not_arm_while_another_app_holds_the_camera(guard):
    # Taking the camera from a video call would be far worse than missing
    # a shoulder surfer, so this outranks every other arming reason.
    set_foreground(guard, "keepassxc.exe")
    set_webcam_users(guard, ["Zoom.exe"])
    guard._evaluate()
    assert guard._worker.armed is False
    assert "Zoom.exe" in guard.state().reason


def test_manual_watch_also_yields_the_camera(guard):
    set_webcam_users(guard, ["Teams.exe"])
    guard.set_manual_watch(True)
    assert guard._worker.armed is False


def test_disarms_when_another_app_takes_the_camera(guard):
    set_foreground(guard, "keepassxc.exe")
    guard._evaluate()
    assert guard._worker.armed is True
    set_webcam_users(guard, ["Zoom.exe"])
    guard._evaluate()
    assert guard._worker.armed is False


def test_rearms_once_the_other_app_releases_it(guard):
    set_foreground(guard, "keepassxc.exe")
    set_webcam_users(guard, ["Zoom.exe"])
    guard._evaluate()
    set_webcam_users(guard, [])
    guard._evaluate()
    assert guard._worker.armed is True


def test_our_own_camera_use_does_not_block_us(guard):
    # The consent store lists this process while the guard is watching.
    own = next(iter(guard._own_process_names()))
    set_foreground(guard, "keepassxc.exe")
    set_webcam_users(guard, [own])
    guard._evaluate()
    assert guard._worker.armed is True


# ---- raising a warning ---------------------------------------------------


def test_one_frame_with_two_faces_does_not_warn(guard, arbiter):
    # The whole point of hysteresis: a single odd frame is not an alarm.
    guard._state.armed = True
    sample(guard, 2, times=1)
    assert arbiter.current().key != "privacy.shoulder"


def test_sustained_second_face_raises_a_warning(guard, arbiter, settings):
    guard._state.armed = True
    sample(guard, 2, times=int(settings.get("privacy.confirm_frames")))
    content = arbiter.current()
    assert content.key == "privacy.shoulder"
    assert content.priority == Priority.PRIVACY
    assert "1 other person" in content.title


def test_a_warning_outranks_everything_else(guard, arbiter):
    from core.model import IslandContent

    arbiter.publish(
        IslandContent(key="alert.cpu", priority=Priority.ALERT, title="busy")
    )
    guard._state.armed = True
    sample(guard, 2, times=3)
    assert arbiter.current().key == "privacy.shoulder"


def test_three_faces_are_described_correctly(guard, arbiter):
    guard._state.armed = True
    sample(guard, 3, times=3)
    assert "2 other people" in arbiter.current().title


def test_a_single_clear_frame_does_not_cancel_a_warning(guard, arbiter):
    guard._state.armed = True
    sample(guard, 2, times=3)
    sample(guard, 1, times=1)
    assert arbiter.current().key == "privacy.shoulder"


def test_sustained_clear_frames_cancel_the_warning(guard, arbiter, settings):
    guard._state.armed = True
    sample(guard, 2, times=3)
    sample(guard, 1, times=int(settings.get("privacy.clear_frames")))
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.shoulder" not in keys


def test_interrupted_runs_do_not_accumulate(guard, arbiter):
    # Two faces, one face, two faces must not add up to a confirmation.
    guard._state.armed = True
    sample(guard, 2, times=2)
    sample(guard, 1, times=1)
    sample(guard, 2, times=2)
    assert arbiter.current().key != "privacy.shoulder"


# ---- dismissal -----------------------------------------------------------


def test_dismiss_clears_and_suppresses(guard, arbiter):
    guard._state.armed = True
    sample(guard, 2, times=3)
    guard._dismiss()
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.shoulder" not in keys
    # Re-confirming immediately must stay quiet, or dismissing is useless.
    sample(guard, 2, times=5)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.shoulder" not in keys


def test_suppression_expires(guard, arbiter, settings):
    settings.set("privacy.dismiss_cooldown_seconds", 0.05)
    guard._state.armed = True
    sample(guard, 2, times=3)
    guard._dismiss()
    time.sleep(0.1)
    sample(guard, 2, times=3)
    assert arbiter.current().key == "privacy.shoulder"


# ---- walk away -----------------------------------------------------------


def test_walk_away_warns_after_the_timeout(guard, arbiter, settings):
    settings.set("privacy.walk_away_seconds", 1)
    guard._state.armed = True
    guard._state.last_seen_face = time.time() - 5
    sample(guard, 0, times=1)
    content = arbiter.current()
    assert content.key == "privacy.away"
    assert any(a.label == "Lock now" for a in content.actions)


def test_walk_away_does_not_fire_while_someone_is_there(guard, arbiter, settings):
    settings.set("privacy.walk_away_seconds", 1)
    guard._state.armed = True
    guard._state.last_seen_face = time.time() - 5
    sample(guard, 1, times=1)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.away" not in keys


def test_walk_away_clears_when_the_user_returns(guard, arbiter, settings):
    settings.set("privacy.walk_away_seconds", 1)
    guard._state.armed = True
    guard._state.last_seen_face = time.time() - 5
    sample(guard, 0, times=1)
    sample(guard, 1, times=1)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.away" not in keys


def test_walk_away_can_be_disabled(guard, arbiter, settings):
    settings.set("privacy.walk_away_seconds", 0)
    guard._state.armed = True
    guard._state.last_seen_face = time.time() - 600
    sample(guard, 0, times=2)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.away" not in keys


def test_the_guard_never_locks_by_itself(guard, arbiter, settings):
    # Locking is only ever reachable through a button the user presses.
    settings.set("privacy.walk_away_seconds", 1)
    guard._state.armed = True
    guard._state.last_seen_face = time.time() - 5
    sample(guard, 0, times=3)
    content = arbiter.current()
    assert content.key == "privacy.away"
    labels = {a.label for a in content.actions}
    assert labels == {"Lock now", "Dismiss"}


# ---- indicator and teardown ---------------------------------------------


def test_an_indicator_shows_while_watching(guard, arbiter):
    guard._on_armed_changed(True)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.indicator" in keys


def test_the_indicator_is_only_ambient(guard, arbiter):
    # Transparency must not mean the guard owns the island all day.
    guard._on_armed_changed(True)
    indicator = next(
        c for c in arbiter.candidates() if c.key == "privacy.indicator"
    )
    assert indicator.priority == Priority.AMBIENT


def test_the_indicator_disappears_when_disarmed(guard, arbiter):
    guard._on_armed_changed(True)
    guard._on_armed_changed(False)
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.indicator" not in keys


def test_disarming_clears_any_active_warning(guard, arbiter):
    guard._state.armed = True
    sample(guard, 2, times=3)
    set_foreground(guard, "notepad.exe")
    guard._evaluate()
    keys = [c.key for c in arbiter.candidates()]
    assert "privacy.shoulder" not in keys


def test_stop_releases_everything(guard, arbiter):
    guard._state.armed = True
    sample(guard, 2, times=3)
    worker = guard._worker
    guard.stop()
    assert "stop" in worker.history
    assert guard._worker is None
    assert arbiter.candidates() == []
    assert guard.state().enabled is False
