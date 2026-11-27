"""Face counting for the privacy guard.

This module answers exactly one question: how many faces are in this
frame. It never stores an image, never writes one to disk, and never
returns pixels to the caller. The only thing that leaves here is a count
and a confidence, which is the minimum that makes the feature work.

Detection is YuNet: a small CNN shipped as a 230 KB ONNX model and run
through OpenCV's ``FaceDetectorYN``. It is accurate enough that a
confidence threshold means something.

There is deliberately no fallback. The obvious one is a Haar cascade,
which false-positives on bookshelves, patterned curtains and face-like
arrangements of anything; for a feature whose entire job is to interrupt
the user, a detector that cries wolf is worse than no detector. Modern
OpenCV builds no longer ship the cascade files anyway, so the choice is
not even a trade-off any more.

The model is not committed to the repository, because a binary blob in a
source tree is a thing nobody audits. ``scripts/fetch_yunet.py``
downloads it on request and verifies its hash. Without the model the
guard refuses to arm and says why.

OpenCV is imported on first use, never at module import. It is tens of
megabytes of resident memory and the island must not pay for it unless
this feature is actually switched on.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from core import paths

# OpenCV's own recommended operating point for YuNet.
YUNET_SCORE_THRESHOLD = 0.85
YUNET_NMS_THRESHOLD = 0.3
# Frames are downscaled before detection. Faces at a laptop's viewing
# distance are large, so a smaller frame costs nothing in accuracy and
# roughly quarters the CPU time.
DETECT_WIDTH = 320

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"


@dataclass(frozen=True)
class Detection:
    """One detected face. Deliberately carries no image data."""

    confidence: float
    # Normalised 0-1 area of the frame, used only to tell a person at the
    # keyboard from someone further back in the room.
    relative_area: float


@dataclass(frozen=True)
class FrameResult:
    faces: tuple[Detection, ...]
    backend: str

    @property
    def count(self) -> int:
        return len(self.faces)

    @property
    def nearest_area(self) -> float:
        return max((f.relative_area for f in self.faces), default=0.0)


def yunet_model_path() -> Path | None:
    """Where the YuNet model lives, if it has been fetched."""
    for directory in (paths.user_dir() / "models", paths.app_root() / "models"):
        candidate = directory / YUNET_FILENAME
        if candidate.is_file():
            return candidate
    return None


def opencv_available() -> bool:
    """True when OpenCV can be imported, without importing it."""
    import importlib.util

    return importlib.util.find_spec("cv2") is not None


class FaceCounter:
    """Counts faces in frames. Loads its backend on first use."""

    def __init__(self, model_path: Path | None = None) -> None:
        self._model_path = model_path
        self._backend = "none"
        self._detector = None
        self._input_size: tuple[int, int] | None = None
        self._loaded = False
        self._error: str | None = None

    # ---- loading -------------------------------------------------------

    def load(self) -> bool:
        if self._loaded:
            return self._backend != "none"
        self._loaded = True
        try:
            import cv2
        except ImportError as exc:
            self._error = (
                "OpenCV is not installed "
                f"(pip install -r requirements-vision.txt) [{exc}]"
            )
            return False

        model = self._model_path or yunet_model_path()
        if model is None:
            self._error = (
                "the face model is missing; run scripts/fetch_yunet.py"
            )
            return False
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(model),
                "",
                (DETECT_WIDTH, DETECT_WIDTH),
                YUNET_SCORE_THRESHOLD,
                YUNET_NMS_THRESHOLD,
                5000,
            )
        except Exception as exc:
            self._error = f"the face model failed to load: {exc}"
            return False
        self._backend = "yunet"
        return True

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def ready(self) -> bool:
        return self._backend == "yunet"

    # ---- detection -----------------------------------------------------

    def count(self, frame) -> FrameResult:
        """Count faces in a BGR frame. Returns an empty result on failure."""
        if not self.load():
            return FrameResult((), self._backend)
        try:
            import cv2

            height, width = frame.shape[:2]
            if width <= 0 or height <= 0:
                return FrameResult((), self._backend)
            scale = DETECT_WIDTH / float(width)
            if scale < 1.0:
                small = cv2.resize(
                    frame,
                    (DETECT_WIDTH, max(1, int(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                small = frame
            small_h, small_w = small.shape[:2]
            area = float(small_w * small_h) or 1.0

            if self._input_size != (small_w, small_h):
                self._detector.setInputSize((small_w, small_h))
                self._input_size = (small_w, small_h)
            _, raw = self._detector.detect(small)
            faces = []
            if raw is not None:
                for row in raw:
                    box_w, box_h = float(row[2]), float(row[3])
                    faces.append(
                        Detection(
                            confidence=float(row[-1]),
                            relative_area=(box_w * box_h) / area,
                        )
                    )
            return FrameResult(tuple(faces), self._backend)
        except Exception:
            # A detection failure must never propagate into the worker
            # loop; a dropped frame is harmless.
            return FrameResult((), self._backend)


class Camera:
    """A webcam held open only while the guard is armed.

    Opening and closing per frame would take about a second each time and
    would blink the hardware privacy light on and off, which looks like a
    malfunction. Holding it open means the light stays on for as long as
    the guard is watching, which is the honest signal and the one the user
    should see.
    """

    def __init__(self, index: int = 0) -> None:
        self._index = index
        self._capture = None
        self._error: str | None = None

    def open(self) -> bool:
        if self._capture is not None:
            return True
        try:
            import cv2

            # DSHOW opens far faster than the default backend on Windows
            # and avoids a multi-second stall on some integrated cameras.
            capture = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                self._error = "could not open the camera"
                return False
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self._capture = capture
            self._error = None
            return True
        except Exception as exc:
            self._error = f"camera unavailable: {exc}"
            return False

    def read(self):
        """One frame, or None. Never raises."""
        if self._capture is None:
            return None
        try:
            ok, frame = self._capture.read()
            return frame if ok else None
        except Exception:
            return None

    def close(self) -> None:
        if self._capture is None:
            return
        with contextlib.suppress(Exception):
            self._capture.release()
        self._capture = None

    @property
    def is_open(self) -> bool:
        return self._capture is not None

    @property
    def error(self) -> str | None:
        return self._error
