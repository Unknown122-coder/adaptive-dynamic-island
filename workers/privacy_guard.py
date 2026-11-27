"""Shoulder-surfing and walk-away detection.

This is the feature with the most ways to go wrong, so the design is
mostly about restraint.

**It is off until asked for.** Nothing here runs unless the user enables
it, and OpenCV is not even imported until then.

**It arms narrowly.** The camera is opened only while a window the user
nominated as sensitive is in front, or while they have explicitly asked
the guard to watch. The rest of the time the device is closed and the
hardware light is off.

**It yields the camera.** Before arming, and continuously afterwards, the
guard checks the Windows capability consent store to see whether another
application is using the webcam. If one is, it does not arm, and if one
starts while it is armed, it disarms. Fighting a video call for the
camera would be a far worse outcome than missing a shoulder surfer.

**It requires persistence.** A warning needs several consecutive frames
agreeing, and clearing needs several frames too. A single bad frame can
neither raise an alarm nor silence one. This is what keeps the feature
from interrupting someone every time a reflection catches the lens.

**It never locks anything by itself.** Walk-away offers a button. A false
negative that locks a machine mid-sentence would do more damage in a day
than the feature prevents in a year.

No frame is stored, written to disk, or passed outside the worker thread.
Only a face count and a size ratio cross the thread boundary.
"""

from __future__ import annotations

import contextlib
import ctypes
import threading
import time
from dataclasses import dataclass

import psutil
from PySide6.QtCore import QObject, QThread, Signal

from core import vision
from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.model import Action, IslandContent, Priority, Urgency
from core.osbridge.consent import ConsentWatcher
from core.osbridge.window_hooks import ForegroundInfo, ForegroundWatcher

# Faces smaller than this fraction of the frame are too far away to be
# reading anything, and are usually a face-shaped something across a room.
MIN_FACE_AREA = 0.012


@dataclass
class GuardState:
    enabled: bool = False
    armed: bool = False
    manual: bool = False
    reason: str = "disabled"
    faces: int = 0
    last_seen_face: float = 0.0
    backend: str = "none"
    error: str | None = None


class _VisionWorker(QThread):
    """Owns the camera and the detector. The only thread that sees pixels."""

    sampled = Signal(int, float)   # face count, largest face as frame fraction
    failed = Signal(str)
    armed_changed = Signal(bool)

    def __init__(self, interval_seconds: float = 1.0) -> None:
        super().__init__()
        self._interval = max(0.2, float(interval_seconds))
        self._arm = threading.Event()
        self._stop = threading.Event()
        self._backend = "none"

    # ---- control (main thread) -----------------------------------------

    def arm(self) -> None:
        self._arm.set()

    def disarm(self) -> None:
        self._arm.clear()

    def request_stop(self) -> None:
        self._stop.set()
        self._arm.set()  # wake the loop so it can exit promptly

    @property
    def backend(self) -> str:
        return self._backend

    # ---- loop (worker thread) -------------------------------------------

    def run(self) -> None:
        counter = vision.FaceCounter()
        camera = vision.Camera()
        was_armed = False
        try:
            while not self._stop.is_set():
                if not self._arm.wait(0.25):
                    if was_armed:
                        camera.close()
                        was_armed = False
                        self.armed_changed.emit(False)
                    continue
                if self._stop.is_set():
                    break

                if not counter.load():
                    self.failed.emit(counter.error or "no face detector")
                    self._arm.clear()
                    continue
                self._backend = counter.backend

                if not camera.is_open:
                    if not camera.open():
                        self.failed.emit(camera.error or "camera unavailable")
                        self._arm.clear()
                        continue
                    was_armed = True
                    self.armed_changed.emit(True)

                frame = camera.read()
                if frame is None:
                    # A dropped frame is normal right after opening the
                    # device; a run of them means the camera went away.
                    self._stop.wait(self._interval)
                    continue

                result = counter.count(frame)
                near = [
                    face
                    for face in result.faces
                    if face.relative_area >= MIN_FACE_AREA
                ]
                self.sampled.emit(len(near), result.nearest_area)
                # The frame goes out of scope here and is never retained.
                self._stop.wait(self._interval)
        finally:
            camera.close()


class PrivacyGuard(QObject):
    """Decides when to watch, and what to do about what it sees."""

    state_changed = Signal()
    warning_raised = Signal(str)

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        foreground: ForegroundWatcher,
        consent: ConsentWatcher,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "privacy")
        self._settings = settings
        self._foreground = foreground
        self._consent = consent
        self._state = GuardState()
        self._worker: _VisionWorker | None = None

        self._consecutive_multi = 0
        self._consecutive_clear = 0
        self._warning_active = False
        self._away_active = False
        self._suppressed_until = 0.0

        foreground.changed.connect(self._on_foreground)
        consent.updated.connect(lambda _snapshot: self._evaluate())

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Enable the guard. Returns False with a reason set if it cannot."""
        if self._state.enabled:
            return True
        if not vision.opencv_available():
            self._state.error = (
                "OpenCV is not installed "
                "(pip install -r requirements-vision.txt)"
            )
            self._publish_unavailable()
            return False
        if vision.yunet_model_path() is None:
            self._state.error = (
                "the face model is missing; run scripts/fetch_yunet.py"
            )
            self._publish_unavailable()
            return False

        self._state.enabled = True
        self._state.error = None
        fps = self._settings.number("privacy.frames_per_second", 1.0)
        self._worker = _VisionWorker(interval_seconds=1.0 / max(0.2, fps))
        self._worker.sampled.connect(self._on_sample)
        self._worker.failed.connect(self._on_failure)
        self._worker.armed_changed.connect(self._on_armed_changed)
        self._worker.start()
        self._evaluate()
        self.state_changed.emit()
        return True

    def stop(self) -> None:
        self._state.enabled = False
        self._state.armed = False
        self._state.manual = False
        if self._worker is not None:
            self._worker.request_stop()
            self._worker.wait(3000)
            self._worker = None
        self._handle.clear()
        self._reset_counters()
        self.state_changed.emit()

    def state(self) -> GuardState:
        return self._state

    def set_manual_watch(self, watching: bool) -> None:
        """The tray's explicit "watch now", independent of the app list."""
        self._state.manual = watching
        self._evaluate()

    # ---- arming decision --------------------------------------------------

    def _own_process_names(self) -> set[str]:
        names = {"adaptiveisland.exe"}
        with contextlib.suppress(psutil.Error, OSError):
            names.add(psutil.Process().name().lower())
        return names

    def _should_arm(self) -> tuple[bool, str]:
        if not self._state.enabled:
            return False, "disabled"
        # Yielding the camera comes before every other consideration.
        if self._consent.webcam_held_by_others(self._own_process_names()):
            users = [u.name for u in self._consent.users("webcam")]
            return False, f"camera in use by {', '.join(users[:2]) or 'another app'}"
        if self._state.manual:
            return True, "watching on request"
        info = self._foreground.current()
        if info is None:
            return False, "no foreground window"
        sensitive = self._settings.lowered_list("privacy.sensitive_processes")
        if not sensitive:
            return False, "no sensitive apps configured"
        if info.process.lower() in sensitive:
            return True, f"{info.process} is in front"
        return False, "no sensitive app in front"

    def _on_foreground(self, _info: ForegroundInfo) -> None:
        self._evaluate()

    def _evaluate(self) -> None:
        should, reason = self._should_arm()
        changed = should != self._state.armed or reason != self._state.reason
        self._state.reason = reason

        if self._worker is None:
            self._state.armed = False
            if changed:
                self.state_changed.emit()
            return

        if should:
            self._worker.arm()
        else:
            self._worker.disarm()
            if self._state.armed:
                self._reset_counters()
                self._handle.retract("shoulder")
                self._handle.retract("away")
                self._warning_active = False
                self._away_active = False
            self._state.armed = False

        self._publish_indicator()
        if changed:
            self.state_changed.emit()

    def _on_armed_changed(self, armed: bool) -> None:
        self._state.armed = armed
        if armed:
            self._state.last_seen_face = time.time()
        self._publish_indicator()
        self.state_changed.emit()

    def _on_failure(self, message: str) -> None:
        self._state.error = message
        self._state.armed = False
        self._publish_unavailable()
        self.state_changed.emit()

    def _reset_counters(self) -> None:
        self._consecutive_multi = 0
        self._consecutive_clear = 0

    # ---- sampling --------------------------------------------------------

    def _on_sample(self, faces: int, _nearest: float) -> None:
        self._state.faces = faces
        self._state.backend = (
            self._worker.backend if self._worker is not None else "none"
        )
        now = time.time()
        if faces >= 1:
            self._state.last_seen_face = now

        threshold = self._settings.integer("privacy.faces_to_warn", 2)
        confirm = self._settings.integer("privacy.confirm_frames", 3)
        clear = self._settings.integer("privacy.clear_frames", 2)

        if faces >= threshold:
            self._consecutive_multi += 1
            self._consecutive_clear = 0
        else:
            self._consecutive_clear += 1
            self._consecutive_multi = 0

        # Hysteresis in both directions: one bad frame can neither raise an
        # alarm nor clear one.
        if (
            self._consecutive_multi >= confirm
            and not self._warning_active
            and now >= self._suppressed_until
        ):
            self._raise_shoulder_warning(faces)
        elif self._warning_active and self._consecutive_clear >= clear:
            self._clear_shoulder_warning()

        self._check_walk_away(now)
        self._publish_indicator()

    def _check_walk_away(self, now: float) -> None:
        seconds = self._settings.number("privacy.walk_away_seconds", 60)
        if seconds <= 0 or self._state.faces > 0:
            if self._away_active and self._state.faces > 0:
                self._handle.retract("away")
                self._away_active = False
            return
        if self._away_active or now < self._suppressed_until:
            return
        if now - self._state.last_seen_face < seconds:
            return

        minutes = int((now - self._state.last_seen_face) // 60)
        detail = (
            f"nobody at the keyboard for {minutes} min"
            if minutes >= 1
            else "nobody at the keyboard"
        )
        self._away_active = True
        self._handle.publish_content(
            IslandContent(
                key="privacy.away",
                priority=Priority.PRIVACY,
                title="You stepped away",
                detail=detail,
                glyph="lock",
                urgency=Urgency.WARNING,
                actions=(
                    Action("Lock now", self.lock_workstation),
                    Action("Dismiss", self._dismiss),
                ),
                auto_expand=True,
                collapsed="stepped away",
            )
        )
        self.warning_raised.emit("walk-away")

    def _raise_shoulder_warning(self, faces: int) -> None:
        self._warning_active = True
        extra = faces - 1
        noun = "person" if extra == 1 else "people"
        self._handle.publish_content(
            IslandContent(
                key="privacy.shoulder",
                priority=Priority.PRIVACY,
                title=f"{extra} other {noun} in view",
                detail="someone may be reading over your shoulder",
                glyph="faces",
                urgency=Urgency.CRITICAL,
                actions=(
                    Action("Lock now", self.lock_workstation),
                    Action("Dismiss", self._dismiss),
                ),
                auto_expand=True,
                collapsed="not alone",
            )
        )
        self.warning_raised.emit("shoulder-surfing")

    def _clear_shoulder_warning(self) -> None:
        self._warning_active = False
        self._handle.retract("shoulder")

    def _dismiss(self) -> None:
        """Silence warnings for a while.

        Without a cooldown, dismissing a warning that is still true just
        makes it reappear immediately, which trains people to disable the
        feature instead of using it.
        """
        seconds = self._settings.number("privacy.dismiss_cooldown_seconds", 120)
        self._suppressed_until = time.time() + seconds
        self._warning_active = False
        self._away_active = False
        self._reset_counters()
        self._state.last_seen_face = time.time()
        self._handle.retract("shoulder")
        self._handle.retract("away")
        self._publish_indicator()

    # ---- actions ---------------------------------------------------------

    @staticmethod
    def lock_workstation() -> bool:
        """Lock the session. Only ever called from a button the user pressed."""
        try:
            return bool(ctypes.WinDLL("user32").LockWorkStation())
        except OSError:
            return False

    # ---- presentation ----------------------------------------------------

    def _publish_indicator(self) -> None:
        """A visible marker whenever the camera is actually open.

        Windows shows its own camera indicator too, but an overlay that
        can open the webcam should say so in its own interface rather than
        relying on the user noticing a tray icon.
        """
        if not self._state.armed:
            self._handle.retract("indicator")
            return
        faces = self._state.faces
        detail = "1 person in view" if faces == 1 else f"{faces} people in view"
        if faces == 0:
            detail = "nobody in view"
        self._handle.publish_content(
            IslandContent(
                key="privacy.indicator",
                priority=Priority.AMBIENT,
                title="Privacy guard watching",
                detail=detail,
                glyph="eye",
                urgency=Urgency.NEUTRAL,
                collapsed="watching",
            )
        )

    def _publish_unavailable(self) -> None:
        self._handle.publish_content(
            IslandContent(
                key="privacy.unavailable",
                priority=Priority.CONTEXT,
                title="Privacy guard unavailable",
                detail=self._state.error or "unknown reason",
                glyph="shield-alert",
                urgency=Urgency.WARNING,
                collapsed="guard unavailable",
                expires_at=ProviderHandle.ttl(10),
            )
        )

    def describe(self) -> str:
        """Lines for the diagnostics panel."""
        state = self._state
        lines = [
            f"  enabled: {state.enabled}",
            f"  armed: {state.armed} ({state.reason})",
            f"  detector: {state.backend}",
            f"  faces last sample: {state.faces}",
        ]
        if state.error:
            lines.append(f"  error: {state.error}")
        model = vision.yunet_model_path()
        lines.append(f"  model: {model if model else 'not installed'}")
        return "\n".join(lines)
