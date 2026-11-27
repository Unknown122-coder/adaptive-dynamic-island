"""Check the privacy guard's camera and detector on your own machine.

The guard's decision logic is covered by the test suite without touching
hardware. This is the other half, and only you can run it: it opens your
webcam, counts faces for a few seconds, and prints what it sees.

    .venv/Scripts/python.exe scripts/check_vision.py [seconds]

Nothing is saved. Frames are counted and discarded inside this process;
no image is written to disk or sent anywhere. Your camera light will be
on for the duration, which is exactly what it should do when something is
using the camera.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import vision
from core.osbridge.consent import capability_users


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    print("Privacy guard self-check\n")

    if not vision.opencv_available():
        print("  [FAIL] OpenCV is not installed")
        print("         pip install -r requirements-vision.txt")
        return 1
    print("  [ok  ] OpenCV is installed")

    model = vision.yunet_model_path()
    if model is None:
        print("  [FAIL] the face model is missing")
        print("         .venv/Scripts/python.exe scripts/fetch_yunet.py")
        return 1
    print(f"  [ok  ] face model at {model}")

    holders = [u.name for u in capability_users("webcam")]
    if holders:
        print(f"  [FAIL] the camera is already in use by {', '.join(holders)}")
        print("         the guard would refuse to arm; close that app first")
        return 1
    print("  [ok  ] no other application is holding the camera")

    counter = vision.FaceCounter()
    if not counter.load():
        print(f"  [FAIL] detector did not load: {counter.error}")
        return 1
    print(f"  [ok  ] detector ready ({counter.backend})")

    camera = vision.Camera()
    if not camera.open():
        print(f"  [FAIL] {camera.error}")
        return 1
    print("  [ok  ] camera opened\n")

    print(f"Watching for {seconds:.0f}s. Your camera light should be on.")
    print("Try stepping out of frame, and try having someone stand behind you.\n")

    samples = 0
    counts: list[int] = []
    worst_latency = 0.0
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            frame = camera.read()
            if frame is None:
                time.sleep(0.2)
                continue
            started = time.perf_counter()
            result = counter.count(frame)
            latency = (time.perf_counter() - started) * 1000
            worst_latency = max(worst_latency, latency)
            near = [
                f for f in result.faces if f.relative_area >= 0.012
            ]
            samples += 1
            counts.append(len(near))
            bar = "#" * len(near) if near else "."
            sizes = ", ".join(f"{f.relative_area:.1%}" for f in near)
            print(
                f"  {len(near)} face(s) {bar:<4} "
                f"{latency:5.1f} ms   {sizes}"
            )
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  interrupted")
    finally:
        camera.close()

    print("\n  camera closed; the light should be off again\n")

    if not samples:
        print("  [FAIL] no frames were captured")
        return 1

    alone = sum(1 for c in counts if c <= 1)
    print(f"  samples          {samples}")
    print(f"  max faces seen   {max(counts)}")
    print(f"  frames alone     {alone}/{samples}")
    print(f"  worst detection  {worst_latency:.1f} ms")

    if max(counts) == 0:
        print("\n  No face was ever detected. Check the camera is not covered,")
        print("  and that you were in frame.")
        return 1
    print("\nOK: the privacy guard can see and count faces on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
